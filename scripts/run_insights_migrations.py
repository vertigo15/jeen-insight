"""Apply insights_*.sql migrations with tracked, once-only, ordered revisions.

Usage:
    docker exec jeen-insights-api python scripts/run_insights_migrations.py

Unlike the previous runner (which re-applied every file on every run and relied
on idempotent DDL), this records each applied revision in
``insights_schema_migrations`` and applies each file at most once, in filename
order, inside its own transaction. This supports non-idempotent changes and
crypto backfills.

After the SQL files, it runs registered Python backfills (e.g. encrypting the
catalog MCP bearer token at rest). A backfill that cannot complete yet (e.g. no
KEK configured) is left unrecorded so it retries on the next run.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Awaitable, Callable, List, Tuple

# Ensure project root is importable when the script runs as a CLI
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.metadata import get_metadata_pool, close_metadata_pool  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations" / "insights"


async def _ensure_history(conn) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS insights_schema_migrations (
            revision    TEXT PRIMARY KEY,
            checksum    TEXT,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


async def _applied(conn) -> set[str]:
    rows = await conn.fetch("SELECT revision FROM insights_schema_migrations")
    return {r["revision"] for r in rows}


async def _apply_sql_files(conn) -> int:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        logger.warning("No migration files found in %s", MIGRATIONS_DIR)
        return 0

    done = await _applied(conn)
    count = 0
    for path in files:
        if path.name in done:
            logger.info("• %s already applied — skipping", path.name)
            continue
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        logger.info("→ Applying %s", path.name)
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO insights_schema_migrations (revision, checksum) "
                "VALUES ($1, $2) ON CONFLICT (revision) DO NOTHING",
                path.name,
                checksum,
            )
        logger.info("  ✅ %s applied", path.name)
        count += 1
    return count


# ── Python backfills (once-only, retry until they can complete) ──────────────

def _opt_in(flag: str) -> bool:
    """True when *flag* env var is an explicit truthy opt-in."""
    return (os.getenv(flag) or "").strip().lower() in ("1", "true", "yes", "on", "t")


async def _backfill_encrypt_mcp_tokens(conn) -> bool:
    """Encrypt any plaintext catalog MCP bearer tokens. Returns True when done.

    Returns False (leave unrecorded, retry later) when the backfill is not opted
    in or no KEK is configured yet.

    IMPORTANT — shared-DB safety: encrypting the catalog MCP token is a one-way,
    KEK-bound operation performed in-place on a row that may be read by MULTIPLE
    deployments sharing the same metadata DB (local dev, the regular Azure stack,
    the defence stack). If this runs from an environment whose APP_ENCRYPTION_KEY
    differs from (or is unknown to) the other readers, those readers can no longer
    decrypt the token and the whole catalog silently goes empty. It also nulls the
    plaintext column, breaking any older code that still reads it.

    Because of that blast radius, the backfill is gated behind an explicit opt-in
    (``ENCRYPT_MCP_TOKENS_BACKFILL=true``). A routine ``run_insights_migrations``
    from a developer machine therefore can never clobber a shared token by
    accident — you must consciously enable it once every reader of that DB shares
    the same KEK.
    """
    from src.security import crypto

    if not _opt_in("ENCRYPT_MCP_TOKENS_BACKFILL"):
        logger.info(
            "backfill(encrypt_mcp_tokens): skipped — set ENCRYPT_MCP_TOKENS_BACKFILL=true "
            "to enable. Leaving the MCP token as-is so shared-DB readers keep working."
        )
        return False

    if not crypto.crypto_available():
        logger.warning(
            "backfill(encrypt_mcp_tokens): APP_ENCRYPTION_KEY not configured — "
            "leaving MCP tokens as-is; will retry once a KEK is set."
        )
        return False

    rows = await conn.fetch(
        "SELECT id, bearer_token FROM insights_mcp_servers "
        "WHERE bearer_token IS NOT NULL AND token_ciphertext IS NULL"
    )
    migrated = 0
    for r in rows:
        async with conn.transaction():
            # Re-check inside the write and re-encrypt the CURRENT plaintext under
            # a row lock so a concurrent rotation cannot be clobbered with a stale
            # token or have its freshly-written ciphertext wiped. The AAD is bound
            # to the row id, so re-encrypting the locked value is correct.
            locked = await conn.fetchrow(
                "SELECT bearer_token FROM insights_mcp_servers "
                "WHERE id = $1 AND token_ciphertext IS NULL FOR UPDATE",
                r["id"],
            )
            if not locked or not locked["bearer_token"]:
                continue  # already encrypted/rotated by someone else — skip
            blob = crypto.encrypt(
                locked["bearer_token"], aad=f"mcp_server:{r['id']}:bearer"
            )
            await conn.execute(
                """
                UPDATE insights_mcp_servers
                   SET token_algo=$2, token_kek_id=$3, token_ciphertext=$4,
                       token_nonce=$5, token_wrapped_dek=$6, token_dek_nonce=$7,
                       bearer_token = NULL, updated_at = NOW()
                 WHERE id = $1 AND token_ciphertext IS NULL
                """,
                r["id"], blob.algo, blob.kek_id, blob.ciphertext,
                blob.nonce, blob.wrapped_dek, blob.dek_nonce,
            )
        migrated += 1
    if migrated:
        logger.info("backfill(encrypt_mcp_tokens): encrypted %d token(s)", migrated)
    return True


_BACKFILLS: List[Tuple[str, Callable[[object], Awaitable[bool]]]] = [
    ("py:encrypt_mcp_tokens_v1", _backfill_encrypt_mcp_tokens),
]


async def _run_backfills(conn) -> None:
    done = await _applied(conn)
    for name, fn in _BACKFILLS:
        if name in done:
            continue
        completed = await fn(conn)
        if completed:
            await conn.execute(
                "INSERT INTO insights_schema_migrations (revision, checksum) "
                "VALUES ($1, NULL) ON CONFLICT (revision) DO NOTHING",
                name,
            )
            logger.info("  ✅ backfill %s recorded", name)


async def run() -> None:
    if not MIGRATIONS_DIR.is_dir():
        raise FileNotFoundError(f"Migrations directory not found: {MIGRATIONS_DIR}")

    pool = await get_metadata_pool()
    try:
        async with pool.acquire() as conn:
            await _ensure_history(conn)
            applied = await _apply_sql_files(conn)
            await _run_backfills(conn)
    finally:
        await close_metadata_pool()

    logger.info(
        "Jeen Insights migrations complete (%d new SQL revision(s) applied).", applied
    )


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        sys.exit(130)
    except Exception:  # noqa: BLE001
        logger.exception("Migration run failed")
        sys.exit(1)
