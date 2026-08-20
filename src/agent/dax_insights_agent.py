"""Jeen Insights text-to-DAX agent (Power BI), parallel to the text-to-SQL one.

``DaxInsightsAgent`` builds the **distinct** DAX LangGraph (``build_dax_graph``)
and invokes it per question. It queries a Power BI dataset via the REST
``executeQueries`` endpoint using the signed-in user's delegated OAuth token
(resolved request-scoped inside the execution node — never stored in state).

It deliberately exposes the same duck-typed surface the shared API routes call
on the SQL agent, so ``/tables``, ``/schema``, charts and insights work with
zero route changes:

  * ``.sql_runner`` → a metadata-backed introspector (``MetadataIntrospector``)
    exposing ``list_tables`` / ``get_table_schema`` from the curated catalog
    (there is no live SQL connection to a Power BI dataset).
  * ``.llm``        → the same ``LangChainLlmService`` used by the graph.

``DaxAgentRegistry`` lazily builds one agent per Power BI ``source_key`` and is
kept entirely separate from the SQL ``AgentRegistry`` so the SQL path is
untouched.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from src.agent.conversation_history import ConversationHistoryService
from src.agent.langgraph_agent.nodes.catalog import _load_catalog_bundle
from src.agent.langgraph_agent.nodes.output import _enrich_trace, slim_trace
from src.agent.langgraph_agent_dax import DaxPromptLoader, build_dax_graph
from src.agent.langgraph_agent_dax.state import DaxAgentState
from src.agent.llm_service import LangChainLlmService
from src.agent.progress import ProgressCallback
from src.agent.user_resolver import SimpleUserResolver
from src.config import settings
from src.connections import Connection, ConnectionService
from src.connectors.powerbi_token import TokenProviderFactory
from src.metadata import MetadataLoader

logger = logging.getLogger(__name__)


def _parse_governed_columns(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [c.strip() for c in raw.split(",") if c.strip()]


# ----------------------------------------------------------------------
# Metadata-backed introspector (duck-types the SqlRunner surface routes use)
# ----------------------------------------------------------------------
class MetadataIntrospector:
    """Read-only ``list_tables`` / ``get_table_schema`` from the curated catalog.

    Power BI datasets have no SQL connection, so ``/tables`` and ``/schema`` are
    served from the metadata DB. The return shapes mirror the SQL runners
    (``list_tables`` → ``List[str]``; ``get_table_schema`` → a list of
    ``{column_name, data_type, is_nullable, description, is_primary_key}`` dicts)
    so the existing routes need no changes.
    """

    def __init__(self, source_key: str, metadata_loader: MetadataLoader) -> None:
        self.source_key = source_key
        self.metadata_loader = metadata_loader
        self.database_type = "powerbi"

    async def list_tables(self) -> List[str]:
        rows = await self.metadata_loader.load_tables_rich(self.source_key)
        return [r["name"] for r in rows if r.get("name")]

    async def get_table_schema(self, table_name: str) -> List[Dict[str, Any]]:
        cols = await self.metadata_loader.load_columns(self.source_key, table_name)
        return [
            {
                "column_name": c.get("column"),
                "data_type": c.get("data_type") or "",
                "is_nullable": "YES" if c.get("is_nullable", True) else "NO",
                "description": c.get("description"),
                "is_primary_key": bool(c.get("is_pk")),
            }
            for c in cols
        ]

    async def close(self) -> None:  # symmetry with SqlRunner; nothing to close.
        return None


# ----------------------------------------------------------------------
# Agent
# ----------------------------------------------------------------------
class DaxInsightsAgent:
    """Per-connection text-to-DAX agent backed by the distinct DAX LangGraph."""

    def __init__(
        self,
        *,
        connection: Connection,
        metadata_loader: MetadataLoader,
        llm_service: LangChainLlmService,
        router_llm_service: LangChainLlmService,
        history_service: ConversationHistoryService,
        user_resolver: SimpleUserResolver,
        prompt_loader: DaxPromptLoader,
        token_provider_factory: Optional[TokenProviderFactory] = None,
    ) -> None:
        self.connection = connection
        self.source_key = connection.source_key
        self.display_name = connection.display_name
        self.database_type = "powerbi"
        self.workspace_id = connection.workspace_id
        self.dataset_id = connection.dataset_id
        self.model_version = connection.model_version
        self.metadata_loader = metadata_loader
        self.history = history_service
        self.user_resolver = user_resolver
        # Duck-typed surfaces the shared API routes call on the SQL agent.
        self.sql_runner = MetadataIntrospector(self.source_key, metadata_loader)
        self.llm = llm_service

        self.graph = build_dax_graph(
            llm=llm_service,
            router_llm=router_llm_service,
            metadata_loader=metadata_loader,
            history_service=history_service,
            prompt_loader=prompt_loader,
            deployment_name=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
            max_retries=settings.DAX_MAX_RETRIES,
            max_history_tokens=settings.LANGGRAPH_MAX_HISTORY_TOKENS,
            dlp_enabled=settings.DLP_ENABLED,
            dax_validation_enabled=settings.DAX_VALIDATION_ENABLED,
            eval_analytics_enabled=settings.EVAL_ANALYTICS_ENABLED,
            require_catalog_for_query=settings.REQUIRE_CATALOG_FOR_QUERY,
            dlp_governed_columns=_parse_governed_columns(settings.DLP_GOVERNED_COLUMNS),
            entity_resolution_enabled=settings.DAX_ENTITY_RESOLUTION_ENABLED,
            entity_max_domain_values=settings.DAX_ENTITY_MAX_DOMAIN_VALUES,
            entity_match_threshold=settings.DAX_ENTITY_MATCH_THRESHOLD,
            entity_cross_column_enabled=settings.DAX_ENTITY_CROSS_COLUMN_ENABLED,
            token_provider_factory=token_provider_factory,
        )
        logger.info(
            "✅ DAX agent ready for source_key=%s (workspace=%s dataset=%s)",
            self.source_key, self.workspace_id, self.dataset_id,
        )

    async def process_question(
        self,
        *,
        question: str,
        session_id: Optional[UUID] = None,
        user_context: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        temperature: Optional[float] = None,
        eval_analytics: Optional[bool] = None,
        llm_timeout: Optional[int] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        """Run the text-to-DAX pipeline for one question."""
        if not session_id:
            session_id = uuid4()

        try:
            user = await self.user_resolver.resolve_user(user_context or {})

            from src.metadata.runtime_settings import get_runtime_settings
            runtime = await get_runtime_settings()

            results = await asyncio.gather(
                _load_catalog_bundle(self.source_key, self.metadata_loader),
                self._fetch_conversation_context(
                    session_id, user_id=str(user.id), limit=runtime.conversation_context_turns
                ),
                self._safe_log_query(
                    user_id=user.id, session_id=session_id, question=question
                ),
                return_exceptions=True,
            )

            metadata_bundle: Dict[str, str] = {}
            catalog_meta: Dict[str, Any] = {}
            if not isinstance(results[0], Exception):
                metadata_bundle, catalog_meta = results[0]
            conversation_context: List[Dict[str, Any]] = (
                results[1] if not isinstance(results[1], Exception) else []
            )
            query_id = results[2] if not isinstance(results[2], Exception) else None

            # A failed catalog pre-load is recoverable — dax_catalog_lookup
            # retries and fails closed with a user-facing message if it also
            # fails — so it must not be seeded into state["error"], which
            # response_formatter would then prefer over a successful answer.
            pre_graph_error: Optional[str] = None
            if isinstance(results[0], Exception):
                logger.error("dax: catalog pre-load failed (recoverable): %s", results[0])
            if isinstance(results[1], Exception):
                logger.warning("dax: _fetch_conversation_context failed: %s", results[1])
            if isinstance(results[2], Exception):
                pre_graph_error = f"Audit log failed: {results[2]}"
                logger.warning("dax: log_query failed (non-fatal): %s", results[2])

            initial_state: DaxAgentState = {
                # ── Input ───────────────────────────────────────────────
                "question": question,
                "session_id": session_id,
                "source_key": self.source_key,
                "user_context": user_context or {},
                "limit": limit,
                "temperature": temperature,
                "progress_callback": progress_callback,
                # ── Connection ──────────────────────────────────────────
                "connection_display_name": self.display_name,
                "database_type": "powerbi",
                "connection_database": None,
                "connection_catalog": None,
                "connection_schema": None,
                # ── Power BI target ─────────────────────────────────────
                "workspace_id": self.workspace_id,
                "dataset_id": self.dataset_id,
                "model_version": self.model_version,
                # ── Audit ───────────────────────────────────────────────
                "query_id": query_id,
                "user_id": str(user.id),
                "start_time": time.monotonic(),
                "llm_call_count": 0,
                "llm_latency_ms": 0,
                "token_usage": {},
                # ── Memory ──────────────────────────────────────────────
                "conversation_history": conversation_context,
                "memory_summary": None,
                "is_over_budget": False,
                # ── Routing ─────────────────────────────────────────────
                "route": "needs_query",
                "route_reason": "",
                # ── Catalog (SQL-shared) ────────────────────────────────
                # Pre-loaded above; dax_catalog_lookup consumes it instead of
                # loading a second time. catalog_seeded is the one-shot ticket.
                "metadata_bundle": metadata_bundle,
                "catalog_seeded": bool(metadata_bundle),
                "catalog_source_used": catalog_meta.get("source", "db"),
                "catalog_cache": catalog_meta.get("cache"),
                "catalog_load_ms": int(catalog_meta.get("load_ms") or 0),
                "dialect_rules": "",
                "known_tables": [],
                "known_columns": [],
                "table_columns": {},
                "catalog_available": False,
                "catalog_error": None,
                "catalog_blocked": False,
                # ── DAX catalog ─────────────────────────────────────────
                "known_measures": [],
                "measure_home_tables": {},
                "measure_dependencies": {},
                "measures_available": False,
                "relationship_graph": [],
                "date_table": None,
                "date_column": None,
                "is_marked_date_table": False,
                # ── Plan ────────────────────────────────────────────────
                "query_plan": None,
                "plan_grain": None,
                "plan_assumptions": [],
                "clarification_required": False,
                # ── Value (entity) linking ──────────────────────────────
                "resolved_entities": [],
                "entity_ambiguities": [],
                "unresolved_entities": [],
                "entity_resolution_attempts": 0,
                # Snapshot the admin-tunable knobs once per question so a mid-
                # flight change cannot alter behaviour between repair retries.
                "entity_resolution_enabled": runtime.dax_entity_resolution_enabled,
                "entity_max_domain_values": runtime.dax_entity_max_domain_values,
                "entity_match_threshold": runtime.dax_entity_match_threshold,
                "entity_cross_column_enabled": runtime.dax_entity_cross_column_enabled,
                # ── DAX generation / validation ─────────────────────────
                "retry_count": 0,
                "generated_sql": None,       # kept for shared nodes; == generated_dax
                "generated_dax": None,
                "clarification": None,
                "error_context": None,
                "identifiers_used": [],
                "defined_measures": [],
                "expected_output_schema": [],
                "expected_grain": None,
                "dax_lint_errors": [],
                "dax_validation_error": None,
                "dax_repairable_error": None,
                "resolved_symbols": {},
                "governed_lineage": [],
                "dlp_blocked": False,
                "governance_error": None,
                # ── Execution / integrity ───────────────────────────────
                "query_result": None,
                "exec_error": None,
                "execution_time_ms": None,
                "http_status": None,
                "pbi_error_code": None,
                "pbi_error_message": None,
                "error_location": None,
                "is_partial": False,
                "is_empty": False,
                "returned_row_count": None,
                "actual_schema": [],
                "retry_after": None,
                "integrity_action": None,
                # ── Evaluation ──────────────────────────────────────────
                "is_trivial": False,
                "eval_result": None,
                # ── DAX retry control ───────────────────────────────────
                "dax_error_category": None,
                "repair_attempts_by_category": {},
                "transport_attempts": 0,
                "plan_regenerations": 0,
                "catalog_refresh_count": 0,
                "empty_diagnostics": 0,
                "previous_dax_hashes": [],
                "dax_feedback_action": None,
                "feedback_type": None,
                "needs_connect": False,
                "dax_terminal": False,
                # ── Per-request overrides ───────────────────────────────
                "eval_analytics_override": eval_analytics,
                "llm_timeout_seconds": (
                    llm_timeout if llm_timeout is not None else settings.LLM_TIMEOUT_SECONDS
                ),
                "max_result_rows": runtime.max_result_rows,
                "statement_timeout_ms": runtime.db_statement_timeout_ms,
                "trace": [],
                "node_prompts": {},
                # ── Output ──────────────────────────────────────────────
                "answer": None,
                "error": pre_graph_error,
            }

            final_state = await self.graph.ainvoke(initial_state)
            formatted = final_state.get("formatted_response") or {}

            raw_trace = list(final_state.get("trace") or [])
            if raw_trace:
                # Slim before enriching: enrichment attaches rendered prompts,
                # so taking the projection first makes leaking one impossible.
                await self._safe_persist_trace(query_id, slim_trace(raw_trace))
                _enrich_trace(raw_trace, final_state)
                formatted["trace"] = raw_trace

            # Surface the connect-required signal so the UI can prompt the user
            # to link their Power BI account (mirrors the connectors flow).
            if final_state.get("needs_connect"):
                formatted.setdefault("needs_connect", True)
                formatted.setdefault("connect_provider", "power-bi")

            return formatted

        except Exception as e:  # noqa: BLE001
            logger.exception("Error processing question via DAX LangGraph")
            return {
                "question": question,
                "query_id": None,
                "session_id": session_id,
                "sql": None,
                "results": None,
                "prompt": None,
                "error": str(e),
                "metrics": None,
            }

    # ------------------------------------------------------------------
    # Internals (mirror the SQL agent)
    # ------------------------------------------------------------------
    async def _safe_log_query(
        self, *, user_id: Any, session_id: UUID, question: str
    ) -> Optional[UUID]:
        return await self.history.log_query(
            user_id=user_id,
            source_key=self.source_key,
            session_id=session_id,
            natural_language_query=question,
            dataset_id=self.source_key,
            rag_context={},
        )

    async def _safe_persist_trace(
        self, query_id: Optional[UUID], node_trace: List[Dict[str, Any]]
    ) -> None:
        """Record per-node timings. Telemetry: never fail the answer over it."""
        if not query_id or not node_trace:
            return
        try:
            await self.history.update_node_trace(query_id=query_id, node_trace=node_trace)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist node trace")

    async def _fetch_conversation_context(
        self, session_id: UUID, *, user_id: str, limit: int = 5
    ) -> List[Dict[str, Any]]:
        try:
            ctx = await self.history.get_conversation_context(
                session_id=session_id, user_id=user_id, limit=limit
            )
            ctx.reverse()
            return ctx
        except Exception:
            logger.exception("dax: failed to fetch conversation context")
            return []


# ----------------------------------------------------------------------
# Registry (separate from the SQL AgentRegistry)
# ----------------------------------------------------------------------
class DaxAgentRegistry:
    """Lazily builds one ``DaxInsightsAgent`` per Power BI ``source_key``.

    Shares the heavy collaborators with the SQL registry (LLM services, metadata
    loader, history, connection service) but is a distinct object so the SQL
    ``AgentRegistry`` path is never touched.
    """

    def __init__(
        self,
        *,
        llm_service: LangChainLlmService,
        router_llm_service: Optional[LangChainLlmService] = None,
        metadata_loader: MetadataLoader,
        connection_service: ConnectionService,
        history_service: ConversationHistoryService,
        user_resolver: SimpleUserResolver,
        prompt_loader: Optional[DaxPromptLoader] = None,
        prompt_cache: Optional[Any] = None,
        token_provider_factory: Optional[TokenProviderFactory] = None,
    ) -> None:
        self.token_provider_factory = token_provider_factory
        self.llm = llm_service
        self.router_llm = router_llm_service or llm_service
        self.metadata_loader = metadata_loader
        self.connection_service = connection_service
        self.history = history_service
        self.user_resolver = user_resolver
        self.prompt_loader = prompt_loader or DaxPromptLoader()
        if prompt_cache is not None:
            self.prompt_loader.attach_cache(prompt_cache)
        self._agents: Dict[str, DaxInsightsAgent] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def get_agent(self, source_key: str) -> DaxInsightsAgent:
        if source_key in self._agents:
            return self._agents[source_key]
        lock = self._locks.setdefault(source_key, asyncio.Lock())
        async with lock:
            if source_key in self._agents:
                return self._agents[source_key]
            connection = await self.connection_service.get_connection(source_key)
            agent = DaxInsightsAgent(
                connection=connection,
                metadata_loader=self.metadata_loader,
                llm_service=self.llm,
                router_llm_service=self.router_llm,
                history_service=self.history,
                user_resolver=self.user_resolver,
                prompt_loader=self.prompt_loader,
                token_provider_factory=self.token_provider_factory,
            )
            self._agents[source_key] = agent
            logger.info("✅ Built DaxInsightsAgent for source_key=%s", source_key)
            return agent

    async def close(self) -> None:
        self._agents.clear()


__all__ = ["DaxInsightsAgent", "DaxAgentRegistry", "MetadataIntrospector"]
