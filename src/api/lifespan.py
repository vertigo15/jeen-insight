"""FastAPI lifespan: builds and tears down shared services.

This is the single source of truth for the app's startup/shutdown order.
Routes never instantiate services themselves; they read from `src.api.state`
(via `src.api.dependencies` getters).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import FastAPI

from src.agent import AgentRegistry, DaxAgentRegistry
from src.agent.conversation_history import ConversationHistoryService
from src.agent.onboarding import OnboardingService
from src.agent.llm_service import LangChainLlmService
from src.agent.prompt_cache import PromptCache
from src.agent.user_resolver import SimpleUserResolver
from src.api import background, state
from src.config import settings
from src.connections import ConnectionService
from src.metadata import (
    MetadataLoader, close_metadata_pool, get_metadata_pool,
    McpServerService, McpCacheService, McpCatalogClient,
)
from src.metadata.insights_schema import ensure_insights_baseline

logger = logging.getLogger(__name__)


def get_agent():
    """Return the AgentRegistry for use by settings hot-reload."""
    return state.agent_registry


async def _ensure_schema(conn) -> None:
    """Create — or, with SCHEMA_BOOTSTRAP_ON_START=false, only verify — the
    Insights-owned baseline tables and columns.

    Delegates to :func:`src.metadata.insights_schema.ensure_insights_baseline`,
    which the migration runner shares, and fails with a clear message when the
    database was not provisioned by Jeen Schema Modeler or (verify mode) the
    migration Job has not run yet.
    """
    apply = bool(settings.SCHEMA_BOOTSTRAP_ON_START)
    logger.info(
        "startup: schema bootstrap %s",
        "enabled (additive DDL if objects are missing)" if apply
        else "disabled (verify only; schema changes go through the migration Job)",
    )
    await ensure_insights_baseline(conn, require_platform=True, apply=apply)


async def _probe_conversation_persistence(conn, history_service: Any) -> None:
    """Enable per-turn artifact capture only when migration 022 is applied.

    The migration runner is a manual step (see README). If the conversation
    tables are missing, the API must keep answering questions without the
    restore feature instead of failing on every turn, so the kill switch is
    forced off for this process and a warning is logged.
    """
    try:
        present = bool(await conn.fetchval(
            "SELECT to_regclass('insights_conversations') IS NOT NULL "
            "AND to_regclass('insights_turn_artifacts') IS NOT NULL "
            "AND to_regclass('insights_conversation_prune_state') IS NOT NULL"
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("startup: conversation schema probe failed: %s", exc)
        present = False
    if not present:
        logger.warning(
            "startup: migration 022_conversations_and_turn_artifacts is not applied; "
            "conversation persistence is DISABLED for this process and turn logging "
            "uses the pre-022 path. Run `python scripts/run_insights_migrations.py` "
            "to enable it."
        )
    # The schema flag drives which SQL the history service runs (the parent FK
    # makes the conversation row mandatory once 022 exists); the kill switch
    # only gates artifact capture and retention on top of that.
    history_service.conversation_schema_ready = present
    enabled = bool(settings.CONVERSATION_PERSISTENCE_ENABLED) and present
    history_service.persistence_enabled = enabled
    logger.info(
        "startup: conversation persistence %s", "enabled" if enabled else "disabled"
    )


async def _probe_analysis_schema(conn) -> bool:
    """True when migration 023 (ML skills) is applied."""
    try:
        return bool(await conn.fetchval(
            "SELECT to_regclass('insights_analysis_proposals') IS NOT NULL "
            "AND to_regclass('insights_user_skill_prefs') IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM information_schema.columns "
            "            WHERE table_name = 'insights_turn_artifacts' AND column_name = 'analysis')"
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("startup: ML skills schema probe failed: %s", exc)
        return False


def _build_analysis_runtime(pool, history_service: Any, analysis_schema_ready: bool) -> None:
    """Wire the ML-skills store and runner into ``state`` (or disable cleanly)."""
    from src.agent.analysis_store import AnalysisStore
    from src.analysis.runner import build_runner

    history_service.analysis_schema_ready = analysis_schema_ready
    if not settings.ML_SKILLS_ENABLED:
        state.analysis_store = None
        state.analysis_runner = None
        logger.info("startup: ML skills disabled (ML_SKILLS_ENABLED=false)")
        return
    if not analysis_schema_ready:
        logger.warning(
            "startup: migration 023_ml_skills is not applied; ML skills run without "
            "proposal persistence or consent memory. Run `python scripts/run_insights_migrations.py`."
        )
    state.analysis_store = AnalysisStore(
        pool, schema_ready=analysis_schema_ready, ttl_seconds=int(settings.ANALYSIS_PROPOSAL_TTL_SECONDS),
    )
    state.analysis_runner = build_runner(settings)
    if state.analysis_runner is None:
        logger.error(
            "startup: no allowed analysis runner (ANALYSIS_RUNNER=%r, JEEN_DEV_MODE=%s); "
            "ML skills will answer with a clear 'not available' message.",
            settings.ANALYSIS_RUNNER, settings.JEEN_DEV_MODE,
        )
    else:
        logger.info("startup: ML skills enabled — runner=%s", getattr(state.analysis_runner, "name", "?"))


async def _analysis_limiter(user_id: str) -> bool:
    limiter = state.rate_limiter
    limit = int(settings.ANALYSIS_RUNS_PER_HOUR_PER_USER or 0)
    if limiter is None or limit <= 0:
        return True
    try:
        return await limiter.allow(f"analysis:{user_id}", limit=limit, window_seconds=3600)
    except Exception:  # noqa: BLE001
        return True


async def _analysis_audit(event: dict) -> None:
    """One append-only audit row per analysis run — never values."""
    audit = state.audit_service
    if audit is None:
        return
    detail = {k: v for k, v in event.items() if k not in ("user_id",)}
    try:
        await audit.log(
            event_type="analysis.run",
            actor_user_id=str(event.get("user_id") or "") or None,
            outcome=str(event.get("outcome") or ""),
            detail=detail,
        )
    except Exception:  # noqa: BLE001
        logger.debug("analysis audit failed", exc_info=True)


async def _warm_caches(
    metadata_loader: Any,
    connection_service: Any,
    prompt_cache: Any,
) -> None:
    """Pre-warm metadata and prompt caches after startup.

    Runs concurrently for all active connections so the first real query
    never pays the cold-start penalty.
    """
    import asyncio as _asyncio
    try:
        connections = await connection_service.list_connections()
        if connections:
            tasks = [metadata_loader.load_all(c.source_key) for c in connections]
            results = await _asyncio.gather(*tasks, return_exceptions=True)
            ok = sum(1 for r in results if not isinstance(r, Exception))
            errors = sum(1 for r in results if isinstance(r, Exception))
            logger.info(
                "startup: pre-warmed metadata for %d connection(s)%s",
                ok,
                f" ({errors} failed)" if errors else "",
            )
        # Warm the system prompt from DB.
        await prompt_cache.get_content("jeen_insights_system")
        logger.info("startup: pre-warmed prompt cache")
    except Exception as exc:  # noqa: BLE001
        logger.warning("startup: cache warm-up skipped: %s", exc)


async def _seed_prompts(conn) -> None:
    """Seed and refresh default prompts in the DB.

    - Inserts a v1 row for any prompt that has no active row yet.
    - Updates the content of non-custom rows whose file has changed since the
      row was last written (e.g. after a code update).  Custom rows are never
      touched so user edits are always preserved.
    """
    # Import here to avoid a circular import at module load time.
    from src.api.routes.settings import PROMPT_REGISTRY

    seeded = updated = 0
    for entry in PROMPT_REGISTRY:
        place = entry["name"]
        path  = entry["path"]
        file_content = path.read_text(encoding="utf-8") if path.exists() else ""

        row = await conn.fetchrow(
            "SELECT id, content, is_custom "
            "FROM insights_prompts WHERE prompt_place = $1 AND is_active = true",
            place,
        )

        if not row:
            # New prompt — insert default v1 row.
            await conn.execute(
                """
                INSERT INTO insights_prompts
                    (prompt_place, content, version, is_active, is_custom, model_id)
                VALUES ($1, $2, 1, true, false, NULL)
                """,
                place,
                file_content,
            )
            seeded += 1
        elif not row["is_custom"] and row["content"] != file_content:
            # Default row whose source file was updated — refresh in place.
            await conn.execute(
                "UPDATE insights_prompts SET content = $1, updated_at = NOW() "
                "WHERE id = $2",
                file_content,
                row["id"],
            )
            updated += 1

    if seeded:
        logger.info("startup: seeded %d prompt row(s) in insights_prompts", seeded)
    if updated:
        logger.info("startup: refreshed %d default prompt row(s) from updated files", updated)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialise services on app startup; close them on shutdown."""
    logger.info("🚀 Starting Jeen Insights...")

    # Fail fast on unsafe internal-auth config (weak/missing signing secret).
    from src.security.internal_auth import assert_configured as _assert_internal_auth
    _assert_internal_auth()
    # Fail fast if APP_ENCRYPTION_KEY is set but weak.
    from src.security.crypto import assert_kek_valid as _assert_kek
    _assert_kek()

    pool = await get_metadata_pool()
    _app.state.map_tile_client = httpx.AsyncClient(
        timeout=max(0.1, float(settings.OSM_TILE_TIMEOUT_SECONDS)),
        follow_redirects=False,
        limits=httpx.Limits(max_connections=40, max_keepalive_connections=12),
    )

    state.metadata_loader    = MetadataLoader(pool)
    state.connection_service  = ConnectionService(pool)
    state.history_service     = ConversationHistoryService(pool)
    state.onboarding_service  = OnboardingService(pool)
    state.mcp_server_service  = McpServerService(pool)
    state.mcp_cache_service   = McpCacheService(pool)
    state.mcp_catalog_client  = McpCatalogClient(
        state.mcp_server_service, state.mcp_cache_service
    )

    # ── Connector / integration platform services ───────────────────────────
    from src.connectors.identity_service import IdentityService
    from src.connectors.registry_service import ConnectorRegistryService
    from src.connectors.grant_service import GrantService
    from src.connectors.snapshot_service import SnapshotService
    from src.connectors.audit_service import AuditService
    from src.connectors.tool_result_service import ToolResultService
    from src.connectors.rate_limiter import RateLimiter
    from src.connectors.action_gate import ActionGate

    state.identity_service = IdentityService(pool)
    state.registry_service = ConnectorRegistryService(pool)
    state.grant_service    = GrantService(pool)
    state.snapshot_service = SnapshotService(pool)
    state.audit_service    = AuditService(pool)
    state.tool_result_service = ToolResultService(pool)
    state.rate_limiter     = RateLimiter(pool)
    state.action_gate      = ActionGate(
        pool,
        registry=state.registry_service,
        grants=state.grant_service,
        snapshots=state.snapshot_service,
        identities=state.identity_service,
        audit=state.audit_service,
        tool_results=state.tool_result_service,
        rate_limiter=state.rate_limiter,
    )

    # ── Schema + prompt seeding ─────────────────────────────────────────────
    async with pool.acquire() as conn:
        await _ensure_schema(conn)
        await _seed_prompts(conn)
        await _probe_conversation_persistence(conn, state.history_service)
        analysis_schema_ready = await _probe_analysis_schema(conn)
    _build_analysis_runtime(pool, state.history_service, analysis_schema_ready)

    # ── Build LLM service from DB credentials ─────────────────────────────────
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM app_settings WHERE key = 'active_model'"
        )
    active_model: Optional[str] = row["value"] if row else None

    try:
        llm_service = await LangChainLlmService.from_db(pool, active_model)
    except Exception as exc:
        logger.warning(
            "startup: DB model load failed (%s); falling back to env-var Azure creds", exc
        )
        llm_service = LangChainLlmService.from_env_azure(pool, settings)

    state.llm_service = llm_service

    # ── Optional cheaper router/memory model ─────────────────────────────────
    # The router, memory-summarizer and memory-answer nodes are cheap
    # classification/condensation calls that don't need the strong SQL model.
    # When AZURE_OPENAI_ROUTER_DEPLOYMENT is set (and differs from the active
    # model) route those nodes to it, saving cost/latency. Falls back to the
    # main model whenever the cheaper one can't be built.
    router_llm_service = llm_service
    router_deployment = (settings.AZURE_OPENAI_ROUTER_DEPLOYMENT or "").strip()
    if router_deployment and router_deployment != llm_service.get_deployment():
        try:
            router_llm_service = await LangChainLlmService.from_db(pool, router_deployment)
            logger.info(
                "startup: router/memory nodes using cheaper model %r (DB)", router_deployment
            )
        except Exception as db_exc:  # noqa: BLE001
            try:
                router_llm_service = LangChainLlmService.from_env_azure(
                    pool, settings, deployment_override=router_deployment
                )
                logger.info(
                    "startup: router/memory nodes using Azure deployment %r (env)",
                    router_deployment,
                )
            except Exception as env_exc:  # noqa: BLE001
                logger.warning(
                    "startup: router model %r unavailable (%s / %s); reusing main model",
                    router_deployment, db_exc, env_exc,
                )
                router_llm_service = llm_service
    state.router_llm_service = router_llm_service

    # ── Warm the model-health cache in the background ────────────────────────
    # Probes every enabled model so the settings UI shows real status straight
    # away and auto-fallback has data without paying a probe cost on first
    # failure. Fire-and-forget — never blocks or fails startup.
    async def _warm_model_health() -> None:
        try:
            from src.agent import llm_health
            health = await llm_health.get_health(pool)
            healthy = [n for n, h in health.items() if h.healthy is True]
            logger.info(
                "startup: model health probed — %d/%d healthy (%s)",
                len(healthy), len(health), ", ".join(sorted(healthy)) or "none",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("startup: model health warm-up failed: %s", exc)

    state.health_warmup_task = background.spawn(
        _warm_model_health(), name="model_health_warmup"
    )

    # ── Prompt cache (starts empty; fills lazily on first use) ───────────────
    state.prompt_cache = PromptCache(pool, llm_service)

    _user_resolver = SimpleUserResolver()
    state.agent_registry = AgentRegistry(
        llm_service=llm_service,
        router_llm_service=router_llm_service,
        prompt_cache=state.prompt_cache,
        metadata_loader=state.metadata_loader,
        connection_service=state.connection_service,
        history_service=state.history_service,
        user_resolver=_user_resolver,
        analysis_store=state.analysis_store,
        # A provider (not the instance) so a runner swapped at runtime is picked up.
        analysis_runner_provider=lambda: state.analysis_runner,
        analysis_limiter=_analysis_limiter,
        analysis_audit=_analysis_audit,
    )

    # Separate registry for Power BI (text-to-DAX) connections. Shares the same
    # collaborators but is a distinct object so the SQL path is never touched.
    # The token factory is built here, where the connector services already
    # exist, so the DAX nodes receive their Power BI credentials instead of
    # reaching back into this module for them.
    from src.connectors.powerbi_token import make_provider_factory

    # Shared with the conversation rerun service, which re-executes stored
    # DAX under the caller's delegated grant without going through the graph.
    state.powerbi_token_provider_factory = make_provider_factory(
        identity_service=state.identity_service,
        registry_service=state.registry_service,
        grant_service=state.grant_service,
    )
    state.dax_agent_registry = DaxAgentRegistry(
        llm_service=llm_service,
        router_llm_service=router_llm_service,
        prompt_cache=state.prompt_cache,
        metadata_loader=state.metadata_loader,
        connection_service=state.connection_service,
        history_service=state.history_service,
        user_resolver=_user_resolver,
        token_provider_factory=state.powerbi_token_provider_factory,
    )

    # ── Build LangGraph insights eval subgraph ────────────────────────────
    try:
        from src.agent.langgraph_agent import build_insights_eval_graph
        state.insights_eval_graph = build_insights_eval_graph(
            llm_service, state.prompt_cache
        )
        logger.info("✅ insights_eval_graph ready")
    except ImportError:
        logger.warning(
            "startup: langgraph not installed — insights eval graph disabled. "
            "Add 'langgraph>=0.2.0' to requirements.txt and rebuild the image."
        )
        state.insights_eval_graph = None
    except Exception as exc:  # noqa: BLE001
        logger.warning("startup: insights_eval_graph build failed: %s", exc)
        state.insights_eval_graph = None

    # ── Pre-warm MCP L1 cache from DB (if MCP mode is active) ────────────────
    try:
        catalog_src = await state.mcp_server_service.get_catalog_source()
        if catalog_src == "mcp":
            active_srv = await state.mcp_server_service.get_active()
            if active_srv:
                warmed = await state.mcp_cache_service.warm_from_db(active_srv.id)
                logger.info("startup: MCP mode active (%s) — warmed %d cache entries", active_srv.server_name, warmed)
            else:
                logger.warning("startup: catalog_source=mcp but no active server; falling back to DB")
        else:
            logger.info("startup: DB catalog mode")
    except Exception as exc:  # noqa: BLE001
        logger.warning("startup: MCP cache warm-up skipped: %s", exc)

    # ── Pre-warm caches (metadata + system prompt) for all connections ────
    await _warm_caches(state.metadata_loader, state.connection_service, state.prompt_cache)

    logger.info("✅ Jeen Insights ready")
    try:
        yield
    finally:
        logger.info("👋 Shutting down Jeen Insights")
        tile_client = getattr(_app.state, "map_tile_client", None)
        if tile_client is not None:
            await tile_client.aclose()
            _app.state.map_tile_client = None
        # Drain detached work (retention prunes, health warm-up) before the
        # pool goes away so no task is left holding a connection mid-flight.
        await background.shutdown()
        state.health_warmup_task = None
        if state.agent_registry:
            await state.agent_registry.close()
        if state.dax_agent_registry:
            await state.dax_agent_registry.close()
        await close_metadata_pool()
        # Reset handles so a hot-reload cycle doesn't leave stale references.
        state.agent_registry       = None
        state.dax_agent_registry   = None
        state.powerbi_token_provider_factory = None
        state.metadata_loader       = None
        state.connection_service    = None
        state.history_service       = None
        state.onboarding_service    = None
        state.llm_service           = None
        state.router_llm_service    = None
        state.prompt_cache          = None
        state.insights_eval_graph   = None
        state.mcp_server_service    = None
        state.mcp_cache_service     = None
        state.mcp_catalog_client    = None
        state.identity_service      = None
        state.registry_service      = None
        state.grant_service         = None
        state.snapshot_service      = None
        state.audit_service         = None
        state.tool_result_service   = None
        state.rate_limiter          = None
        state.action_gate           = None
        state.analysis_store        = None
        state.analysis_runner       = None
