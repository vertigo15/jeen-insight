"""DB-backed end-to-end test for the agent-tools connector gate.

Unlike the unit tests (which fake the DB), this drives the REAL services against
a REAL Postgres so the SQL, envelope encryption, atomic audit, rate counters,
result-snapshot authorization, and the propose -> preview -> execute (+continue)
state machine are all exercised together.

External network egress is the ONLY thing stubbed: each provider adapter's
``execute`` is patched to return a canned provider response, so no real Graph /
Slack / Jira / Tavily call is made. Everything else — including OAuth token
selection, scope checks, snapshot rendering authorization, artifact encryption,
and rate limiting — runs for real.

Run it against an ephemeral Postgres (see the module docstring in the PR):

    docker run -d --name jeen-e2e-pg -e POSTGRES_USER=e2e -e POSTGRES_PASSWORD=e2e \
        -e POSTGRES_DB=e2e -p 55440:5432 postgres:16-alpine
    # apply migrations (creates app_settings first), then:
    JEEN_E2E_DB=1 APP_ENCRYPTION_KEY=<base64-32B> JEEN_DEV_MODE=true \
      METADATA_DB_HOST=localhost METADATA_DB_PORT=55440 METADATA_DB_NAME=e2e \
      METADATA_DB_USER=e2e METADATA_DB_PASSWORD=e2e METADATA_DB_SSL=false \
      python3 -m pytest tests/integration/test_connector_gate_db.py -q

The whole module SKIPS unless ``JEEN_E2E_DB`` is truthy and a strong KEK is
configured, so the normal (DB-less) unit run is unaffected.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

_ENABLED = (os.getenv("JEEN_E2E_DB") or "").strip().lower() in ("1", "true", "yes", "on")

pytestmark = pytest.mark.skipif(
    not _ENABLED, reason="Set JEEN_E2E_DB=1 (+ test METADATA_DB_* / APP_ENCRYPTION_KEY) to run"
)

OWNER = "e2e-owner-1"
ACTOR_EMAIL = "user@corp.com"


# ── canned provider responses (the ONLY egress that is stubbed) ────────────────

async def _stub_write(self, *, action, params, snapshot_payload, config,
                      access_token=None, api_key=None):
    # A real write path would POST to Graph/Slack/Jira here; assert the gate handed
    # us exactly what the policy promised, then report "accepted".
    assert snapshot_payload is not None, "write actions must receive the snapshot"
    return {"accepted": True, "status_code": 202}


async def _stub_tavily(self, *, action, params, snapshot_payload, config,
                       access_token=None, api_key=None):
    assert api_key, "api_key actions must receive the admin key"
    return {
        "accepted": True,
        "status_code": 200,
        "data": {"query": params.get("query"),
                 "results": [{"title": "T", "url": "https://example.com", "content": "hello"}]},
    }


class _FakeLLM:
    """Minimal LLM stand-in for the read continuation (tools MUST be disabled)."""

    def __init__(self):
        self.calls = []

    async def generate(self, messages, tools=None, temperature=0.2):
        self.calls.append({"messages": messages, "tools": tools})
        return {"content": "Answer composed from the fenced web data."}


async def _reset_connector(registry, key: str):
    existing = await registry.get_by_key(key)
    if existing:
        await registry.delete_connector(existing["id"])


async def _mk_connector(registry, *, key, enable=True, config=None):
    await _reset_connector(registry, key)
    c = await registry.create_connector(catalog_key=key, display_name=None, created_by="admin")
    if config:
        await registry.set_config(c["id"], config, created_by="admin")
    if enable:
        await registry.set_enabled(c["id"], True)
    return await registry.get_connector(c["id"])


async def _active_grant(grants, *, identity_id, connector, scopes):
    now = datetime.now(timezone.utc)
    return await grants.upsert_grant(
        identity_id=identity_id,
        connector_id=connector["id"],
        connector_version_id=connector["current_version"]["id"],
        external_account=ACTOR_EMAIL,
        scopes=scopes,
        refresh_token="refresh-xyz",
        access_token="access-xyz",
        access_expires_at=now + timedelta(hours=1),
    )


async def _build_services():
    """Build the real services against the (env-configured) metadata DB.

    The pool is created lazily on the CURRENT event loop (pytest-asyncio uses a
    fresh loop per test), so callers must build + close it within one test.
    """
    from src.security import crypto

    if not crypto.crypto_available():
        pytest.skip("APP_ENCRYPTION_KEY not configured for the e2e DB")

    from src.metadata import get_metadata_pool
    from src.connectors.identity_service import IdentityService
    from src.connectors.registry_service import ConnectorRegistryService
    from src.connectors.grant_service import GrantService
    from src.connectors.snapshot_service import SnapshotService
    from src.connectors.audit_service import AuditService
    from src.connectors.tool_result_service import ToolResultService
    from src.connectors.rate_limiter import RateLimiter
    from src.connectors.action_gate import ActionGate

    pool = await get_metadata_pool()
    registry = ConnectorRegistryService(pool)
    identities = IdentityService(pool)
    grants = GrantService(pool)
    snapshots = SnapshotService(pool)
    audit = AuditService(pool)
    tool_results = ToolResultService(pool)
    rate = RateLimiter(pool)
    gate = ActionGate(
        pool, registry=registry, grants=grants, snapshots=snapshots,
        identities=identities, audit=audit, tool_results=tool_results, rate_limiter=rate,
    )
    return {
        "pool": pool, "registry": registry, "identities": identities,
        "grants": grants, "snapshots": snapshots, "audit": audit,
        "tool_results": tool_results, "gate": gate,
    }


async def _audit_count(pool, proposal_id, event_type):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM connector_audit WHERE proposal_id=$1 AND event_type=$2",
            proposal_id, event_type,
        )


async def _rate_rows(pool):
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM connector_rate_counters")


# ─────────────────────────────────────────────────────────────────────────────

async def test_full_agent_tools_flow_against_real_db():
    from src.connectors.action_gate import ActionError
    from src.connectors.providers.graph_mail import GraphMailAdapter
    from src.connectors.providers.slack import SlackAdapter
    from src.connectors.providers.jira import JiraAdapter
    from src.connectors.providers.tavily import TavilyAdapter
    from src.metadata import close_metadata_pool
    from src.security import app_flags

    services = await _build_services()
    try:
        await _run_full_flow(services, ActionError, GraphMailAdapter, SlackAdapter,
                             JiraAdapter, TavilyAdapter, app_flags)
    finally:
        await close_metadata_pool()


async def _run_full_flow(_services, ActionError, GraphMailAdapter, SlackAdapter,
                         JiraAdapter, TavilyAdapter, app_flags):
    pool = _services["pool"]
    registry = _services["registry"]
    identities = _services["identities"]
    grants = _services["grants"]
    snapshots = _services["snapshots"]
    tool_results = _services["tool_results"]
    gate = _services["gate"]

    # Both master switches ON (agent tool-calling requires both).
    await app_flags.set_connectors_enabled(True)
    await app_flags.set_agent_tools_enabled(True)

    # Connectors (Slack pinned to a single allowed channel to exercise the policy).
    mail = await _mk_connector(registry, key="microsoft-graph-mail")
    slack = await _mk_connector(registry, key="slack-message",
                                config={"allowed_channels": ["#allowed"]})
    jira = await _mk_connector(registry, key="jira-issue")
    tavily = await _mk_connector(registry, key="tavily-web-search")
    await registry.set_api_key(tavily["id"], "tvly-secret-key", created_by="admin")

    # Identity + entitlement (local allow exception is enough; group sync not needed).
    identity = await identities.upsert_identity(
        tenant_id="tenant-1", object_id="obj-1", upn=ACTOR_EMAIL, display_name="User"
    )
    iid = identity["id"]
    for c in (mail, slack, jira, tavily):
        await identities.add_local_exception(
            identity_id=iid, effect="allow", scope="connector",
            connector_id=c["id"], created_by="admin",
        )

    # Active OAuth grants with the exact scopes the policies require.
    await _active_grant(grants, identity_id=iid, connector=mail, scopes="Mail.Send")
    await _active_grant(grants, identity_id=iid, connector=slack, scopes="chat:write")
    await _active_grant(grants, identity_id=iid, connector=jira, scopes="write:jira-work")

    # A server-held result snapshot owned by this principal (the write authorization).
    snap = await snapshots.create_snapshot(
        owner_user_id=OWNER, identity_id=iid, connection="conn-1", query_id="q-1",
        sql="select 1", results={"columns": ["a", "b"], "rows": [[1, 2], [3, 4]]},
    )
    assert snap and snap["row_count"] == 2

    with patch.object(GraphMailAdapter, "execute", _stub_write), \
         patch.object(SlackAdapter, "execute", _stub_write), \
         patch.object(JiraAdapter, "execute", _stub_write), \
         patch.object(TavilyAdapter, "execute", _stub_tavily):

        # ── EMAIL: propose (agent) -> preview -> execute ────────────────────────
        p = await gate.propose(
            owner_user_id=OWNER, identity_id=iid, connector_id=mail["id"],
            action="send_email", snapshot_id=snap["id"],
            params={"recipients": ["alice@corp.com"], "subject": "Q result"},
            origin="agent",
        )
        # execute BEFORE preview is refused (must confirm first).
        with pytest.raises(ActionError) as ei:
            await gate.execute(proposal_id=p["proposal_id"], nonce=p["nonce"],
                               owner_user_id=OWNER, actor_email=ACTOR_EMAIL, params={})
        assert ei.value.status_code == 428

        pv = await gate.preview(proposal_id=p["proposal_id"], nonce=p["nonce"],
                                owner_user_id=OWNER,
                                params={"recipients": ["alice@corp.com"], "subject": "Q result"})
        assert pv["recipients"] == ["alice@corp.com"]
        assert pv["has_external"] is False

        ex = await gate.execute(proposal_id=p["proposal_id"], nonce=p["nonce"],
                                owner_user_id=OWNER, actor_email=ACTOR_EMAIL, params={})
        assert ex["accepted"] is True
        # Atomic audit: 'attempted' and 'succeeded' both recorded for this proposal.
        assert await _audit_count(pool, p["proposal_id"], "action.attempted") == 1
        assert await _audit_count(pool, p["proposal_id"], "action.succeeded") == 1
        # Re-execute is refused (single-execution claim).
        with pytest.raises(ActionError) as ei2:
            await gate.execute(proposal_id=p["proposal_id"], nonce=p["nonce"],
                               owner_user_id=OWNER, actor_email=ACTOR_EMAIL, params={})
        assert ei2.value.status_code == 409

        # ── EMAIL policy: an out-of-domain recipient is rejected at preview ─────
        p_bad = await gate.propose(
            owner_user_id=OWNER, identity_id=iid, connector_id=mail["id"],
            action="send_email", snapshot_id=snap["id"],
            params={"recipients": ["x@corp.com"], "subject": "S"}, origin="agent",
        )
        with pytest.raises(ActionError):
            await gate.preview(proposal_id=p_bad["proposal_id"], nonce=p_bad["nonce"],
                               owner_user_id=OWNER,
                               params={"recipients": ["stranger@outside.com"], "subject": "S"})

        # ── SLACK: allowlist enforced, then a permitted channel succeeds ───────
        p_s = await gate.propose(
            owner_user_id=OWNER, identity_id=iid, connector_id=slack["id"],
            action="post_message", snapshot_id=snap["id"],
            params={"channel": "#allowed"}, origin="agent",
        )
        with pytest.raises(ActionError):
            await gate.preview(proposal_id=p_s["proposal_id"], nonce=p_s["nonce"],
                               owner_user_id=OWNER, params={"channel": "#not-allowed"})
        await gate.preview(proposal_id=p_s["proposal_id"], nonce=p_s["nonce"],
                           owner_user_id=OWNER, params={"channel": "#allowed"})
        ex_s = await gate.execute(proposal_id=p_s["proposal_id"], nonce=p_s["nonce"],
                                  owner_user_id=OWNER, actor_email=ACTOR_EMAIL, params={})
        assert ex_s["accepted"] is True

        # ── JIRA: create issue ─────────────────────────────────────────────────
        p_j = await gate.propose(
            owner_user_id=OWNER, identity_id=iid, connector_id=jira["id"],
            action="create_issue", snapshot_id=snap["id"],
            params={"project_key": "ABC", "issue_type": "Task", "summary": "From Jeen"},
            origin="agent",
        )
        await gate.preview(proposal_id=p_j["proposal_id"], nonce=p_j["nonce"],
                           owner_user_id=OWNER,
                           params={"project_key": "ABC", "issue_type": "Task", "summary": "From Jeen"})
        ex_j = await gate.execute(proposal_id=p_j["proposal_id"], nonce=p_j["nonce"],
                                  owner_user_id=OWNER, actor_email=ACTOR_EMAIL, params={})
        assert ex_j["accepted"] is True

        # ── TAVILY read: no snapshot / no grant; artifact + continuation ───────
        p_t = await gate.propose(
            owner_user_id=OWNER, identity_id=iid, connector_id=tavily["id"],
            action="web_search", params={"query": "latest on X", "max_results": 5},
            origin="agent",
        )
        await gate.preview(proposal_id=p_t["proposal_id"], nonce=p_t["nonce"],
                           owner_user_id=OWNER, params={"query": "latest on X"})
        ex_t = await gate.execute(proposal_id=p_t["proposal_id"], nonce=p_t["nonce"],
                                  owner_user_id=OWNER, actor_email=ACTOR_EMAIL, params={})
        assert ex_t["kind"] == "read"
        artifact_id = ex_t["continuation"]["artifact_id"]
        assert artifact_id

        # Response-only continuation: real artifact decrypt + fence, tools DISABLED.
        from src.agent.read_continuation import continue_read
        fake_llm = _FakeLLM()
        cont = await continue_read(
            proposal_id=p_t["proposal_id"], artifact_id=artifact_id,
            question="latest on X", owner_user_id=OWNER, session_id=None,
            tool_results=tool_results, llm=fake_llm,
        )
        assert cont["tools_disabled"] is True
        assert cont["answer"]
        assert fake_llm.calls and fake_llm.calls[0]["tools"] is None  # tools disabled

        # Single-consume: the artifact is gone after the continuation.
        again = await tool_results.consume(artifact_id, owner_user_id=OWNER, session_id=None)
        assert again is None

    # Distributed rate counters were written for the executes above.
    assert await _rate_rows(pool) > 0

    # ── Kill switch: flipping agent_tools_enabled OFF is IMMEDIATE (read fresh) ─
    await app_flags.set_agent_tools_enabled(False)
    with pytest.raises(ActionError) as ei_k:
        await gate.propose(
            owner_user_id=OWNER, identity_id=iid, connector_id=tavily["id"],
            action="web_search", params={"query": "q"}, origin="agent",
        )
    assert ei_k.value.status_code == 403
    await app_flags.set_agent_tools_enabled(True)
