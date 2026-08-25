"""Jeen Insights agent: LangGraph-based text-to-SQL orchestrator.

``JeenInsightsAgent`` builds a compiled LangGraph graph on initialisation and
invokes it for every incoming question.  The graph handles memory management,
routing, SQL generation, validation, governance, retry recovery, result
evaluation, response formatting, history persistence, and observability.

``AgentRegistry`` lazily builds one ``JeenInsightsAgent`` per ``source_key``
and shares the heavy collaborators (LLM services, metadata pool, history
service, connection service, prompt loader) across them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from src.agent.conversation_history import ConversationHistoryService
from src.agent.langgraph_agent import PromptLoader, build_graph
from src.agent.langgraph_agent.nodes.catalog import _load_catalog_bundle
from src.agent.langgraph_agent.nodes.output import _enrich_trace, slim_trace
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import LangChainLlmService
from src.agent.progress import ProgressCallback
from src.agent.user_resolver import SimpleUserResolver
from src.config import settings
from src.connections import Connection, ConnectionService
from src.metadata import MetadataLoader
from src.connectors import SqlRunner

# Type alias — avoids a hard import of PromptCache at module level
_PromptCache = Any

logger = logging.getLogger(__name__)


def _parse_governed_columns(raw: Optional[str]) -> List[str]:
    """Split the comma-separated DLP_GOVERNED_COLUMNS setting into a clean list."""
    if not raw:
        return []
    return [c.strip() for c in raw.split(",") if c.strip()]


# ----------------------------------------------------------------------
# Agent
# ----------------------------------------------------------------------
class JeenInsightsAgent:
    """Per-connection text-to-SQL agent backed by a LangGraph state graph."""

    def __init__(
        self,
        *,
        connection: Connection,
        sql_runner: SqlRunner,
        llm_service: LangChainLlmService,
        router_llm_service: LangChainLlmService,
        metadata_loader: MetadataLoader,
        history_service: ConversationHistoryService,
        user_resolver: SimpleUserResolver,
        prompt_loader: PromptLoader,
    ):
        self.connection = connection
        self.source_key = connection.source_key
        self.display_name = connection.display_name
        self.database_type = connection.database_type
        self.metadata_loader = metadata_loader
        self.history = history_service
        self.user_resolver = user_resolver
        self.sql_runner = sql_runner
        self.llm = llm_service           # used by charts.py + insights.py routes

        self.graph = build_graph(
            llm=llm_service,
            router_llm=router_llm_service,
            sql_runner=sql_runner,
            metadata_loader=metadata_loader,
            history_service=history_service,
            prompt_loader=prompt_loader,
            deployment_name=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
            max_retries=settings.LANGGRAPH_MAX_RETRIES,
            max_history_tokens=settings.LANGGRAPH_MAX_HISTORY_TOKENS,
            dlp_enabled=settings.DLP_ENABLED,
            sqlglot_validation_enabled=settings.SQLGLOT_VALIDATION_ENABLED,
            eval_analytics_enabled=settings.EVAL_ANALYTICS_ENABLED,
            require_catalog_for_query=settings.REQUIRE_CATALOG_FOR_QUERY,
            enforce_schema_qualifier=settings.SCHEMA_QUALIFIER_VALIDATION_ENABLED,
            dlp_governed_columns=_parse_governed_columns(settings.DLP_GOVERNED_COLUMNS),
            filter_resolution_enabled=settings.SQL_FILTER_RESOLUTION_ENABLED,
            filter_max_domain_values=settings.SQL_FILTER_MAX_DOMAIN_VALUES,
            filter_match_threshold=settings.SQL_FILTER_MATCH_THRESHOLD,
        )
        logger.info(
            "✅ LangGraph agent ready for source_key=%s", self.source_key
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
        """Run the LangGraph text-to-SQL pipeline.

        ``limit`` and ``temperature`` are optional per-request overrides
        sourced from the user's settings panel.  ``None`` means "use the
        server's default".  Server-side bounds are enforced by the Pydantic
        request schema.
        """
        if not session_id:
            session_id = uuid4()

        try:
            user = await self.user_resolver.resolve_user(user_context or {})

            # Live-editable global guardrails (DB statement timeout, row cap,
            # conversation-context window). Cached briefly inside the service.
            from src.metadata.runtime_settings import get_runtime_settings
            runtime = await get_runtime_settings()

            # ── Parallel DB round-trips ──────────────────────────────────────
            # metadata_loader, conversation history, and query audit log are
            # all independent — run them concurrently to save ~1-2s of Azure
            # network latency on every request.
            #
            # log_query is non-fatal: if the audit insert fails (e.g. DB
            # hiccup) the flow continues with query_id=None and the error is
            # surfaced in the UI via formatted_response["error"].
            results = await asyncio.gather(
                _load_catalog_bundle(
                    self.source_key,
                    self.metadata_loader,
                    question=question,
                ),
                self._fetch_conversation_context(
                    session_id, user_id=str(user.id), limit=runtime.conversation_context_turns
                ),
                self._safe_log_query(
                    user_id=user.id,
                    session_id=session_id,
                    question=question,
                ),
                return_exceptions=True,
            )

            # Unpack — keep going even if individual calls errored
            metadata_bundle: Dict[str, str] = {}
            catalog_meta: Dict[str, Any] = {}
            if not isinstance(results[0], Exception):
                metadata_bundle, catalog_meta = results[0]
            conversation_context: List[Dict[str, Any]] = (
                results[1] if not isinstance(results[1], Exception) else []
            )
            query_id = (
                results[2] if not isinstance(results[2], Exception) else None
            )

            # Surface non-fatal pre-graph errors for observability.
            #
            # A failed catalog load is deliberately NOT one of them: this load is
            # only a head start for catalog_lookup, which will retry and, if it
            # also fails, fail closed with a message written for the user. Seeding
            # state["error"] here would outlive that recovery — response_formatter
            # prefers state["error"] over everything else, so a transient blip
            # would surface as an error on an otherwise successful answer.
            pre_graph_error: Optional[str] = None
            if isinstance(results[0], Exception):
                logger.error("catalog pre-load failed (recoverable): %s", results[0])
            if isinstance(results[1], Exception):
                logger.warning("_fetch_conversation_context failed: %s", results[1])
            if isinstance(results[2], Exception):
                pre_graph_error = f"Audit log failed: {results[2]}"
                logger.warning("log_query failed (non-fatal): %s", results[2])

            initial_state: AgentState = {
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
                "database_type": self.database_type,
                "connection_database": self.connection.connection_database,
                "connection_catalog": self.connection.connection_catalog,
                "connection_schema": self.connection.db_schema,
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
                # ── Catalog ─────────────────────────────────────────────
                # Pre-loaded above; catalog_lookup consumes it instead of
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
                # ── Filter planning / grounding ──────────────────────────
                "filter_plan": None,
                "resolved_filters": [],
                "unresolved_filters": [],
                "filter_ambiguities": [],
                "filter_clarification_required": False,
                "filter_resolution_attempts": 0,
                "empty_filter_diagnostics": 0,
                "needs_filter_reground": False,
                "filter_resolution_enabled": runtime.sql_filter_resolution_enabled,
                "filter_max_domain_values": runtime.sql_filter_max_domain_values,
                "filter_match_threshold": runtime.sql_filter_match_threshold,
                # ── SQL loop ────────────────────────────────────────────
                "retry_count": 0,
                "generated_sql": None,
                "clarification": None,
                "error_context": None,
                # ── Validation ──────────────────────────────────────────
                "sqlglot_error": None,
                "dlp_blocked": False,
                "governance_error": None,
                # ── Execution ───────────────────────────────────────────
                "query_result": None,
                "exec_error": None,
                "execution_time_ms": None,
                # ── Evaluation ──────────────────────────────────────────
                "is_trivial": False,
                "eval_result": None,
                # ── Feedback ────────────────────────────────────────────
                "feedback_type": None,
                # ── Per-request overrides ──────────────────────────────────────
                "eval_analytics_override": eval_analytics,
                "llm_timeout_seconds": (
                    llm_timeout if llm_timeout is not None else settings.LLM_TIMEOUT_SECONDS
                ),
                "max_result_rows": runtime.max_result_rows,
                "statement_timeout_ms": runtime.db_statement_timeout_ms,
                # Empty list — operator.add in AgentState accumulates across nodes
                "trace": [],
                # Empty dict — each LLM node adds its rendered prompt here
                "node_prompts": {},
                # ── Output ─────────────────────────────────────────────────────────────────
                "answer": None,
                # Surface pre-graph errors (e.g. audit log failure) in the UI
                # response without stopping the query flow.
                "error": pre_graph_error,
            }

            final_state = await self.graph.ainvoke(initial_state)
            formatted = final_state.get("formatted_response") or {}

            # Attach the COMPLETE execution trace from the final state. This must
            # happen here (not in response_formatter) so the tail nodes that run
            # after the formatter — response_formatter, save_to_memory and
            # observability_log — are included in the developer log.
            raw_trace = list(final_state.get("trace") or [])
            if raw_trace:
                # Slim before enriching: enrichment attaches rendered prompts,
                # so taking the projection first makes leaking one impossible.
                await self._safe_persist_trace(query_id, slim_trace(raw_trace))
                _enrich_trace(raw_trace, final_state)
                formatted["trace"] = raw_trace

            return formatted

        except Exception as e:  # noqa: BLE001
            logger.exception("Error processing question via LangGraph")
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
    # Internals
    # ------------------------------------------------------------------

    async def _safe_log_query(
        self,
        *,
        user_id: Any,
        session_id: UUID,
        question: str,
    ) -> Optional[UUID]:
        """Insert the query audit record.  Returns the new query_id or raises
        so the caller (``asyncio.gather``) can handle the failure gracefully."""
        return await self.history.log_query(
            user_id=user_id,
            source_key=self.source_key,
            session_id=session_id,
            natural_language_query=question,
            dataset_id=self.source_key,
            rag_context={},  # metadata not yet available; parallel fetch
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
            ctx.reverse()  # chronological order, oldest first
            if ctx:
                logger.info(
                    "🧠 Short-term memory: %d previous Q&As loaded for %s",
                    len(ctx),
                    self.source_key,
                )
            return ctx
        except Exception:
            logger.exception("Failed to fetch conversation context")
            return []

    def _summarize_metadata(self, bundle: Dict[str, str]) -> Dict[str, int]:
        return {
            key: len([line for line in value.splitlines() if line.startswith("- ")])
            for key, value in bundle.items()
        }


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------
class AgentRegistry:
    """Lazily builds one ``JeenInsightsAgent`` per ``source_key``.

    Accepts either:
      - ``prompt_loader`` (legacy: reads from disk .md files), OR
      - ``prompt_cache``  (new: DB-backed, used by lifespan.py)
    When ``prompt_cache`` is supplied a fresh ``PromptLoader`` is created
    internally so the graph nodes (which still use ``prompt_loader.render()``)
    continue to work unchanged.

    ``router_llm_service`` is optional; when omitted the main ``llm_service``
    is used for routing as well (simpler single-model setups).
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
        prompt_loader: Optional[PromptLoader] = None,
        prompt_cache: Optional[Any] = None,   # PromptCache — avoids circular import
    ):
        self.llm = llm_service
        self.router_llm = router_llm_service or llm_service
        self.metadata_loader = metadata_loader
        self.connection_service = connection_service
        self.history = history_service
        self.user_resolver = user_resolver
        # Prefer an explicitly supplied PromptLoader; otherwise build one from disk.
        # The graph nodes call prompt_loader.arender() so they always need this.
        self.prompt_loader = prompt_loader or PromptLoader()
        # When a DB-backed PromptCache is available, attach it so the main graph
        # honours Settings-UI prompt edits and per-prompt model overrides (the
        # graph previously read disk files only, diverging from the DB).
        if prompt_cache is not None:
            self.prompt_loader.attach_cache(prompt_cache)
        self._agents: Dict[str, JeenInsightsAgent] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def get_agent(self, source_key: str) -> JeenInsightsAgent:
        if source_key in self._agents:
            return self._agents[source_key]
        lock = self._locks.setdefault(source_key, asyncio.Lock())
        async with lock:
            if source_key in self._agents:
                return self._agents[source_key]
            connection = await self.connection_service.get_connection(source_key)
            runner = await self.connection_service.get_runner(source_key)
            agent = JeenInsightsAgent(
                connection=connection,
                sql_runner=runner,
                llm_service=self.llm,
                router_llm_service=self.router_llm,
                metadata_loader=self.metadata_loader,
                history_service=self.history,
                user_resolver=self.user_resolver,
                prompt_loader=self.prompt_loader,
            )
            self._agents[source_key] = agent
            logger.info("✅ Built JeenInsightsAgent for source_key=%s", source_key)
            return agent

    async def close(self) -> None:
        await self.connection_service.close()
        self._agents.clear()
