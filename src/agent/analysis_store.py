"""Persistence for ML-skill proposals and per-skill consent.

A *proposal* is what the graph hands back instead of a result when it must ask
first (confirm card, clarification, guard refusal). It is the only
server-trusted source of ``{skill, params}`` for ``/api/analysis/run``: the
browser posts the id plus an allowlisted parameter patch, never the params
themselves. Proposals are owner-bound, expire, and are consumed at most once
(an idempotency key makes a retried ``/run`` return the first turn).

Consent ("don't ask again") is scoped to (user, connection, skill,
contract_version), so a contract change re-prompts.

Every method is best-effort against a missing schema (migration 023 not yet
applied): reads return ``None``/``False`` and writes are skipped with a
warning, so questions keep working without the feature.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

import asyncpg

from src.analysis.contracts import CONTRACT_VERSION

logger = logging.getLogger(__name__)


def _jsonb(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


class AnalysisStore:
    def __init__(self, pool: asyncpg.Pool, *, schema_ready: bool = True, ttl_seconds: int = 900) -> None:
        self.pool = pool
        self.schema_ready = schema_ready
        self.ttl_seconds = int(ttl_seconds)

    # ── Proposals ─────────────────────────────────────────────────────────

    async def create_proposal(
        self,
        *,
        user_id: str,
        source_key: str,
        session_id: Optional[UUID],
        parent_query_id: Optional[UUID],
        kind: str,
        skill: str,
        params: Dict[str, Any],
        question: Optional[str],
        proposal: Dict[str, Any],
        contract_version: str = CONTRACT_VERSION,
        idempotency_key: Optional[str] = None,
        consumed: bool = False,
    ) -> Optional[str]:
        """Persist a proposal. ``idempotency_key`` is unique across proposals, so a
        retried re-run collides here (returns None) instead of executing twice.
        ``consumed`` marks a proposal that executes immediately (re-runs)."""
        if not self.schema_ready:
            return None
        pid = uuid4()
        expires = datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO insights_analysis_proposals
                        (id, user_id, source_key, session_id, parent_query_id, kind, skill,
                         params, contract_version, question, proposal, expires_at, idempotency_key, consumed_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11::jsonb, $12, $13,
                            CASE WHEN $14::boolean THEN NOW() END)
                    """,
                    pid, user_id, source_key, session_id, parent_query_id, kind, skill,
                    json.dumps(params, default=str), contract_version, question,
                    json.dumps(proposal, default=str), expires, idempotency_key, bool(consumed),
                )
            return str(pid)
        except asyncpg.UniqueViolationError:
            return None
        except Exception:  # noqa: BLE001
            logger.exception("analysis_store: failed to create proposal")
            return None

    def expires_at_for_new(self) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)).replace(microsecond=0).isoformat()

    async def get_proposal(self, proposal_id: UUID, *, user_id: str, source_key: str) -> Optional[Dict[str, Any]]:
        """Owner-bound lookup. Returns the row (with ``expired``/``consumed`` flags) or None."""
        if not self.schema_ready:
            return None
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT id, user_id, source_key, session_id, parent_query_id, kind, skill,
                           params, contract_version, question, proposal, created_at,
                           expires_at, consumed_at, consumed_query_id, idempotency_key
                    FROM insights_analysis_proposals
                    WHERE id = $1 AND user_id = $2 AND source_key = $3
                    """,
                    proposal_id, user_id, source_key,
                )
        except Exception:  # noqa: BLE001
            logger.exception("analysis_store: failed to load proposal %s", proposal_id)
            return None
        if row is None:
            return None
        now = datetime.now(timezone.utc)
        return {
            "id": str(row["id"]),
            "user_id": row["user_id"],
            "source_key": row["source_key"],
            "session_id": row["session_id"],
            "parent_query_id": row["parent_query_id"],
            "kind": row["kind"],
            "skill": row["skill"],
            "params": _jsonb(row["params"]) or {},
            "contract_version": row["contract_version"],
            "question": row["question"],
            "proposal": _jsonb(row["proposal"]) or {},
            "expired": bool(row["expires_at"] and row["expires_at"] <= now),
            "consumed": row["consumed_at"] is not None,
            "consumed_query_id": str(row["consumed_query_id"]) if row["consumed_query_id"] else None,
            "idempotency_key": row["idempotency_key"],
        }

    async def claim_proposal(
        self, proposal_id: UUID, *, user_id: str, idempotency_key: Optional[str]
    ) -> str:
        """Atomically take the single execution slot of a proposal *before* running.

        Returns ``claimed``, or why not: ``consumed`` (already run or in
        progress), ``expired``, ``not_found``, ``duplicate_key`` (the same
        idempotency key already names another execution). Two concurrent
        ``/run`` calls therefore execute at most once.
        """
        if not self.schema_ready:
            return "claimed"
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    UPDATE insights_analysis_proposals
                       SET consumed_at = NOW(), idempotency_key = COALESCE($3, idempotency_key)
                     WHERE id = $1 AND user_id = $2 AND consumed_at IS NULL AND expires_at > NOW()
                    RETURNING id
                    """,
                    proposal_id, user_id, idempotency_key,
                )
                if row is not None:
                    return "claimed"
                state = await conn.fetchrow(
                    "SELECT consumed_at, expires_at FROM insights_analysis_proposals WHERE id = $1 AND user_id = $2",
                    proposal_id, user_id,
                )
        except asyncpg.UniqueViolationError:
            return "duplicate_key"
        except Exception:  # noqa: BLE001
            logger.exception("analysis_store: failed to claim proposal %s", proposal_id)
            return "not_found"
        if state is None:
            return "not_found"
        if state["consumed_at"] is not None:
            return "consumed"
        return "expired"

    async def release_proposal(self, proposal_id: UUID, *, user_id: str) -> None:
        """Undo a claim whose execution raised, so the user can retry."""
        if not self.schema_ready:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE insights_analysis_proposals SET consumed_at = NULL "
                    "WHERE id = $1 AND user_id = $2 AND consumed_query_id IS NULL",
                    proposal_id, user_id,
                )
        except Exception:  # noqa: BLE001
            logger.exception("analysis_store: failed to release proposal %s", proposal_id)

    async def record_result(self, proposal_id: UUID, *, user_id: str, query_id: Optional[UUID]) -> None:
        """Attach the child turn to a claimed proposal so a retry can point at it."""
        if not self.schema_ready or query_id is None:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE insights_analysis_proposals SET consumed_query_id = $3 WHERE id = $1 AND user_id = $2",
                    proposal_id, user_id, query_id,
                )
        except Exception:  # noqa: BLE001
            logger.exception("analysis_store: failed to record result for %s", proposal_id)

    async def find_by_idempotency_key(self, key: str, *, user_id: str, source_key: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not self.schema_ready or not key:
            return None
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id, consumed_query_id, session_id FROM insights_analysis_proposals "
                    "WHERE idempotency_key = $1 AND user_id = $2 AND ($3::text IS NULL OR source_key = $3)",
                    key, user_id, source_key,
                )
        except Exception:  # noqa: BLE001
            return None
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "query_id": str(row["consumed_query_id"]) if row["consumed_query_id"] else None,
            "session_id": row["session_id"],
        }

    # ── Consent ───────────────────────────────────────────────────────────

    async def has_skill_pref(self, *, user_id: str, source_key: str, skill: str,
                             contract_version: str = CONTRACT_VERSION) -> bool:
        if not self.schema_ready:
            return False
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchval(
                    "SELECT 1 FROM insights_user_skill_prefs "
                    "WHERE user_id = $1 AND source_key = $2 AND skill = $3 AND contract_version = $4",
                    user_id, source_key, skill, contract_version,
                )
            return bool(row)
        except Exception:  # noqa: BLE001
            logger.exception("analysis_store: failed to read skill pref")
            return False

    async def set_skill_pref(self, *, user_id: str, source_key: str, skill: str, remember: bool,
                             contract_version: str = CONTRACT_VERSION) -> None:
        if not self.schema_ready:
            return
        try:
            async with self.pool.acquire() as conn:
                if remember:
                    await conn.execute(
                        """
                        INSERT INTO insights_user_skill_prefs (user_id, source_key, skill, contract_version)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (user_id, source_key, skill, contract_version) DO UPDATE SET confirmed_at = NOW()
                        """,
                        user_id, source_key, skill, contract_version,
                    )
                else:
                    await conn.execute(
                        "DELETE FROM insights_user_skill_prefs "
                        "WHERE user_id = $1 AND source_key = $2 AND skill = $3 AND contract_version = $4",
                        user_id, source_key, skill, contract_version,
                    )
        except Exception:  # noqa: BLE001
            logger.exception("analysis_store: failed to write skill pref")

    async def list_skill_prefs(self, *, user_id: str, source_key: str,
                               contract_version: str = CONTRACT_VERSION) -> Dict[str, bool]:
        if not self.schema_ready:
            return {}
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT skill FROM insights_user_skill_prefs "
                    "WHERE user_id = $1 AND source_key = $2 AND contract_version = $3",
                    user_id, source_key, contract_version,
                )
            return {r["skill"]: True for r in rows}
        except Exception:  # noqa: BLE001
            return {}


class InMemoryAnalysisStore:
    """Test double with the same surface; also the fallback when no pool exists."""

    def __init__(self, *, ttl_seconds: int = 900) -> None:
        self.ttl_seconds = ttl_seconds
        self.schema_ready = True
        self.proposals: Dict[str, Dict[str, Any]] = {}
        self.prefs: set = set()

    def _key_taken(self, key: Optional[str], *, user_id: str, source_key: str, except_id: Optional[str] = None) -> bool:
        return bool(key) and any(
            r.get("idempotency_key") == key and r["user_id"] == user_id and r["source_key"] == source_key
            and r["id"] != except_id
            for r in self.proposals.values()
        )

    async def create_proposal(self, **kw) -> Optional[str]:
        key = kw.pop("idempotency_key", None)
        consumed = bool(kw.pop("consumed", False))
        if self._key_taken(key, user_id=kw.get("user_id", ""), source_key=kw.get("source_key", "")):
            return None
        pid = str(uuid4())
        row = dict(kw)
        row.update({"id": pid, "expires_at": datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds),
                    "consumed_at": datetime.now(timezone.utc) if consumed else None,
                    "consumed_query_id": None, "idempotency_key": key})
        row.setdefault("contract_version", CONTRACT_VERSION)
        self.proposals[pid] = row
        return pid

    def expires_at_for_new(self) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)).replace(microsecond=0).isoformat()

    async def get_proposal(self, proposal_id, *, user_id, source_key):
        row = self.proposals.get(str(proposal_id))
        if not row or row["user_id"] != user_id or row["source_key"] != source_key:
            return None
        return {
            **{k: row.get(k) for k in ("id", "user_id", "source_key", "session_id", "parent_query_id", "kind",
                                        "skill", "params", "contract_version", "question", "proposal")},
            "expired": row["expires_at"] <= datetime.now(timezone.utc),
            "consumed": row["consumed_at"] is not None,
            "consumed_query_id": row["consumed_query_id"],
            "idempotency_key": row["idempotency_key"],
        }

    async def claim_proposal(self, proposal_id, *, user_id, idempotency_key):
        row = self.proposals.get(str(proposal_id))
        if not row or row["user_id"] != user_id:
            return "not_found"
        if row["consumed_at"] is not None:
            return "consumed"
        if row["expires_at"] <= datetime.now(timezone.utc):
            return "expired"
        if self._key_taken(idempotency_key, user_id=user_id, source_key=row["source_key"], except_id=row["id"]):
            return "duplicate_key"
        row["consumed_at"] = datetime.now(timezone.utc)
        row["idempotency_key"] = idempotency_key or row.get("idempotency_key")
        return "claimed"

    async def release_proposal(self, proposal_id, *, user_id):
        row = self.proposals.get(str(proposal_id))
        if row and row["user_id"] == user_id and row["consumed_query_id"] is None:
            row["consumed_at"] = None

    async def record_result(self, proposal_id, *, user_id, query_id):
        row = self.proposals.get(str(proposal_id))
        if row and row["user_id"] == user_id:
            row["consumed_query_id"] = query_id

    async def find_by_idempotency_key(self, key, *, user_id, source_key=None):
        for row in self.proposals.values():
            if (key and row.get("idempotency_key") == key and row["user_id"] == user_id
                    and (source_key is None or row["source_key"] == source_key)):
                return {"id": row["id"], "query_id": str(row["consumed_query_id"]) if row["consumed_query_id"] else None,
                        "session_id": row.get("session_id")}
        return None

    async def has_skill_pref(self, *, user_id, source_key, skill, contract_version=CONTRACT_VERSION):
        return (user_id, source_key, skill, contract_version) in self.prefs

    async def set_skill_pref(self, *, user_id, source_key, skill, remember, contract_version=CONTRACT_VERSION):
        key = (user_id, source_key, skill, contract_version)
        if remember:
            self.prefs.add(key)
        else:
            self.prefs.discard(key)

    async def list_skill_prefs(self, *, user_id, source_key, contract_version=CONTRACT_VERSION):
        return {k[2]: True for k in self.prefs if k[0] == user_id and k[1] == source_key and k[3] == contract_version}
