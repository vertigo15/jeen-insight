"""Apply insights_*.sql migrations with tracked, once-only, ordered revisions.

Usage:
    docker exec jeen-insights-api python scripts/run_insights_migrations.py

Unlike the previous runner (which re-applied every file on every run and relied
on idempotent DDL), this records each applied revision in
``insights_schema_migrations`` and applies each file at most once, in filename
order, inside its own transaction. This supports non-idempotent changes and
crypto backfills.

Before skipping an already-recorded SQL revision, the runner verifies its
stored non-null SHA-256 checksum against the current file. Checksum drift fails
closed unless an operator explicitly sets ``MIGRATION_ALLOW_CHECKSUM_DRIFT=true``.
Historical rows with a NULL checksum remain accepted.

After the SQL files, it runs registered Python backfills (e.g. encrypting the
catalog MCP bearer token at rest). A backfill that cannot complete yet (e.g. no
KEK configured) is left unrecorded so it retries on the next run.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import sys
from pathlib import Path
from typing import Awaitable, Callable, List, Tuple

# Ensure project root is importable when the script runs as a CLI
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.metadata import get_metadata_pool, close_metadata_pool  # noqa: E402
from src.metadata.insights_schema import ensure_insights_baseline  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations" / "insights"

# Session-level advisory lock so concurrent runners (several API replicas
# starting with RUN_MIGRATIONS_ON_START=true, or an operator running the script
# while a pod boots) apply revisions one at a time instead of racing on DDL.
_ADVISORY_LOCK_KEY = "jeen_insights_schema_migrations"

# The metadata DB is shared with Jeen Schema Modeler, so the runner must never
# queue indefinitely behind another session: it bounds how long it waits for
# the advisory lock, for any table lock (lock_timeout) and for any single
# statement (statement_timeout). A timeout aborts the run before the next
# unrecorded revision starts; the already-applied revisions stay recorded.
_LOCK_WAIT_SECONDS = os.getenv("MIGRATION_LOCK_WAIT_SECONDS", "120")
_LOCK_TIMEOUT = os.getenv("MIGRATION_LOCK_TIMEOUT", "15s")
_STATEMENT_TIMEOUT = os.getenv("MIGRATION_STATEMENT_TIMEOUT", "10min")
_LOCK_POLL_SECONDS = 1.0


class MigrationLockTimeout(RuntimeError):
    """Another runner (or a stuck session) held the migration lock too long."""


class MigrationConfigError(ValueError):
    """A MIGRATION_* knob would disable or unbound a safety limit."""


class MigrationChecksumMismatch(RuntimeError):
    """An applied SQL revision no longer matches its recorded checksum."""


def _parse_wait_seconds(raw: str) -> float:
    try:
        value = float(str(raw).strip())
    except ValueError as exc:
        raise MigrationConfigError(f"MIGRATION_LOCK_WAIT_SECONDS={raw!r} is not a number") from exc
    if not math.isfinite(value) or value < 0:
        raise MigrationConfigError(
            f"MIGRATION_LOCK_WAIT_SECONDS={raw!r} must be a finite, non-negative number of seconds"
        )
    return value


async def _configure_session_timeouts(conn) -> None:
    """Apply lock_timeout / statement_timeout for this session.

    Values are validated server-side as positive intervals (a zero interval would
    *disable* the limit) and applied through parameterised ``set_config`` so no
    part of the environment value is ever interpolated into SQL.
    """
    for setting, value in (("lock_timeout", _LOCK_TIMEOUT), ("statement_timeout", _STATEMENT_TIMEOUT)):
        seconds = await conn.fetchval(
            "SELECT EXTRACT(EPOCH FROM $1::text::interval)::float8", value
        )
        if seconds is None or seconds <= 0:
            raise MigrationConfigError(
                f"MIGRATION_{setting.upper()}={value!r} must be a positive interval (e.g. '15s')"
            )
        await conn.fetchval("SELECT set_config($1, $2, false)", setting, value)
    # Keep every unqualified migration/baseline object in the Insights schema.
    # set_config is parameterised so neither the setting nor its value is
    # interpolated into SQL. PostgreSQL still searches pg_catalog implicitly
    # before this explicit path.
    await conn.fetchval("SELECT set_config($1, $2, false)", "search_path", "public")
    logger.info(
        "session timeouts: lock_timeout=%s statement_timeout=%s | search_path=%s",
        _LOCK_TIMEOUT, _STATEMENT_TIMEOUT,
        await conn.fetchval("SELECT current_setting('search_path')"),
    )


async def _acquire_migration_lock(conn, *, wait_seconds: float | str = _LOCK_WAIT_SECONDS) -> None:
    """Take the advisory lock, polling with pg_try_advisory_lock up to *wait_seconds*."""
    wait_seconds = _parse_wait_seconds(wait_seconds)
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        if await conn.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", _ADVISORY_LOCK_KEY):
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise MigrationLockTimeout(
                f"could not acquire the migration lock within {wait_seconds:g}s — another "
                "runner is still applying revisions, or a session is holding the lock. "
                "Check pg_locks / pg_stat_activity and retry."
            )
        logger.info("migration lock is held by another session — waiting…")
        await asyncio.sleep(_LOCK_POLL_SECONDS)


async def _ensure_history(conn) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public.insights_schema_migrations (
            revision    TEXT PRIMARY KEY,
            checksum    TEXT,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


async def _history_exists(conn) -> bool:
    """Check for migration history without creating or altering anything."""
    return bool(
        await conn.fetchval(
            "SELECT to_regclass($1) IS NOT NULL",
            "public.insights_schema_migrations",
        )
    )


async def _applied(conn) -> dict[str, str | None]:
    rows = await conn.fetch(
        "SELECT revision, checksum FROM public.insights_schema_migrations"
    )
    return {r["revision"]: r["checksum"] for r in rows}


def _migration_checksum(path: Path) -> tuple[str, str]:
    sql = path.read_text(encoding="utf-8")
    return sql, hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _verify_applied_checksum(
    revision: str, stored_checksum: str | None, current_checksum: str
) -> None:
    if stored_checksum is None:
        logger.warning(
            "• %s already applied with a historical NULL checksum — accepting",
            revision,
        )
        return
    if stored_checksum == current_checksum:
        return
    message = (
        f"checksum mismatch for applied migration {revision}: "
        f"database={stored_checksum} current={current_checksum}"
    )
    if not _opt_in("MIGRATION_ALLOW_CHECKSUM_DRIFT"):
        raise MigrationChecksumMismatch(
            f"{message}. Refusing to continue; restore the original SQL file or, "
            "only after an explicit review, set MIGRATION_ALLOW_CHECKSUM_DRIFT=true."
        )
    logger.critical(
        "CHECKSUM DRIFT OVERRIDE ENABLED: %s; skipping the modified applied revision",
        message,
    )


async def _preflight_applied_sql(conn) -> None:
    """Validate complete on-disk history before any baseline DDL can run."""
    paths = {path.name: path for path in MIGRATIONS_DIR.glob("*.sql")}
    done = await _applied(conn)
    recorded_sql = {revision for revision in done if revision.endswith(".sql")}
    missing = sorted(recorded_sql - paths.keys())
    if missing:
        raise MigrationChecksumMismatch(
            "recorded SQL migration file(s) missing from the image: "
            + ", ".join(missing)
            + ". Applied migrations are append-only; restore the exact files."
        )
    for revision in sorted(recorded_sql):
        _, checksum = _migration_checksum(paths[revision])
        _verify_applied_checksum(revision, done[revision], checksum)


async def _apply_sql_files(conn, *, checksums_preflighted: bool = False) -> int:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        logger.warning("No migration files found in %s", MIGRATIONS_DIR)
        return 0

    done = await _applied(conn)
    count = 0
    for path in files:
        sql, checksum = _migration_checksum(path)
        if path.name in done:
            if not checksums_preflighted:
                _verify_applied_checksum(path.name, done[path.name], checksum)
            logger.info("• %s already applied — skipping", path.name)
            continue
        logger.info("→ Applying %s", path.name)
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO public.insights_schema_migrations (revision, checksum) "
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
                "INSERT INTO public.insights_schema_migrations (revision, checksum) "
                "VALUES ($1, NULL) ON CONFLICT (revision) DO NOTHING",
                name,
            )
            logger.info("  ✅ backfill %s recorded", name)


async def run() -> None:
    if not MIGRATIONS_DIR.is_dir():
        raise FileNotFoundError(f"Migrations directory not found: {MIGRATIONS_DIR}")
    _parse_wait_seconds(_LOCK_WAIT_SECONDS)  # fail on bad config before connecting

    pool = await get_metadata_pool()
    try:
        async with pool.acquire() as conn:
            await _configure_session_timeouts(conn)
            await _acquire_migration_lock(conn)
            try:
                history_exists = await _history_exists(conn)
                if history_exists:
                    # Fail before ensure_insights_baseline can issue any DDL.
                    await _preflight_applied_sql(conn)
                # Revisions 012/017 INSERT into app_settings, which the API
                # otherwise creates at start-up; create the baseline first so a
                # fresh database can be migrated before the API ever ran.
                await ensure_insights_baseline(conn, require_platform=False, lock_timeout=None)
                await _ensure_history(conn)
                applied = await _apply_sql_files(
                    conn, checksums_preflighted=history_exists
                )
                await _run_backfills(conn)
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", _ADVISORY_LOCK_KEY)
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
