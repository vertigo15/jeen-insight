"""Database-level checks for the prompt-governance audit trail.

Run with the same ephemeral PostgreSQL configuration as the other opt-in
database integration tests:

    JEEN_E2E_DB=1 METADATA_DB_* ... python3 -m pytest \
        tests/integration/test_prompt_governance_db.py -q
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

_ENABLED = (os.getenv("JEEN_E2E_DB") or "").strip().lower() in ("1", "true", "yes", "on")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _ENABLED,
        reason="Set JEEN_E2E_DB=1 (+ test METADATA_DB_*) to run",
    ),
]


@pytest.mark.asyncio
async def test_prompt_audit_trigger_rejects_update_and_delete():
    """The migration must make audit records append-only in PostgreSQL itself."""
    from asyncpg.exceptions import RaiseError
    from src.metadata import close_metadata_pool, get_metadata_pool

    migration = (
        Path(__file__).resolve().parents[2]
        / "db"
        / "migrations"
        / "insights"
        / "020_prompt_governance.sql"
    ).read_text(encoding="utf-8")
    pool = await get_metadata_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(migration)
            audit_id = await conn.fetchval(
                """
                INSERT INTO insights_prompt_audit
                    (prompt_place, action, actor_user_id, actor_email, version)
                VALUES ($1, 'save', 'test-admin', 'test@example.invalid', 1)
                RETURNING id
                """,
                f"test-audit-{uuid4()}",
            )
            with pytest.raises(RaiseError, match="append-only"):
                await conn.execute(
                    "UPDATE insights_prompt_audit SET action = 'tampered' WHERE id = $1",
                    audit_id,
                )
            with pytest.raises(RaiseError, match="append-only"):
                await conn.execute(
                    "DELETE FROM insights_prompt_audit WHERE id = $1",
                    audit_id,
                )
    finally:
        await close_metadata_pool()
