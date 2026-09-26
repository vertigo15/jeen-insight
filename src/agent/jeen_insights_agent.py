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
from typing import Any, Callable, Dict, List, Optional
from uuid import UUID, uuid4

from src.agent.conversation_history import ConversationHistoryService
from src.agent.langgraph_agent import PromptLoader, build_graph
from src.agent.langgraph_agent.nodes.catalog import _extract_columns, _load_catalog_bundle
from src.agent.langgraph_agent.nodes.filtering import ValueLookupPrefetch, start_value_lookup_prefetch
from src.agent.langgraph_agent.nodes.output import _enrich_trace, slim_trace
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import LangChainLlmService
from src.agent.progress import PartialCallback, ProgressCallback, emit_pre_graph
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
        analysis_store: Any = None,
        analysis_runner_provider: Any = None,
        analysis_limiter: Any = None,
        analysis_audit: Any = None,
        usage_ledger: Any = None,
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
        self.analysis_store = analysis_store
        self.usage_ledger = usage_ledger

        self.graph = build_graph(
            llm=llm_service,
            router_llm=router_llm_service,
            sql_runner=sql_runner,
            metadata_loader=metadata_loader,
            history_service=history_service,
            prompt_loader=prompt_loader,
            deployment_name=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
            max_retries=settings.LANGGRAPH_MAX_RETRIES,
            dlp_enabled=settings.DLP_ENABLED,
            sqlglot_validation_enabled=settings.SQLGLOT_VALIDATION_ENABLED,
            eval_analytics_enabled=settings.EVAL_ANALYTICS_ENABLED,
            empty_recheck_enabled=settings.LANGGRAPH_EMPTY_RECHECK,
            require_catalog_for_query=settings.REQUIRE_CATALOG_FOR_QUERY,
            enforce_schema_qualifier=settings.SCHEMA_QUALIFIER_VALIDATION_ENABLED,
            dlp_governed_columns=_parse_governed_columns(settings.DLP_GOVERNED_COLUMNS),
            filter_resolution_enabled=settings.SQL_FILTER_RESOLUTION_ENABLED,
            filter_max_domain_values=settings.SQL_FILTER_MAX_DOMAIN_VALUES,
            filter_match_threshold=settings.SQL_FILTER_MATCH_THRESHOLD,
            filter_lookup_timeout_ms=settings.SQL_FILTER_LOOKUP_TIMEOUT_MS,
            filter_cache_ttl_seconds=settings.SQL_FILTER_CACHE_TTL_SECONDS,
            ml_skills_enabled=bool(settings.ML_SKILLS_ENABLED),
            analysis_store=analysis_store,
            analysis_runner_provider=analysis_runner_provider,
            analysis_limiter=analysis_limiter,
            analysis_audit=analysis_audit,
            analysis_max_series_rows=int(settings.ANALYSIS_MAX_SERIES_ROWS),
            analysis_max_entity_rows=int(settings.ANALYSIS_MAX_ENTITY_ROWS),
            memory_compute_max_rows=int(settings.MEMORY_COMPUTE_MAX_ROWS),
            memory_max_bound_values=int(settings.MEMORY_MAX_BOUND_VALUES),
            usage_ledger=usage_ledger,
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
        analysis_enabled: Optional[bool] = None,
        filter_choices: Optional[List[Dict[str, Any]]] = None,
        partial_callback: Optional[PartialCallback] = None,
        answer_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """Run the LangGraph text-to-SQL pipeline.

        ``limit`` and ``temperature`` are optional per-request overrides
        sourced from the user's settings panel.  ``None`` means "use the
        server's default".  Server-side bounds are enforced by the Pydantic
        request schema. ``analysis_enabled=False`` keeps this one question on
        the SQL path even when it reads like an ML request. ``filter_choices``
        carries the user's answers to earlier column/value clarifications
        (``{literal, table, column, any}``) so the grounder honours them
        instead of asking again. ``partial_callback`` receives the accepted
        rows before the narration LLM call (streaming route only).
        ``answer_callback`` receives the finished response, with the trace up
        to that point, before the history writes run (streaming route only);
        the returned response then carries the complete trace, with the steps
        that ran after the answer marked ``after_answer``.
        """
        extra: Dict[str, Any] = {}
        if analysis_enabled is not None:
            extra["analysis_enabled_override"] = analysis_enabled
        if filter_choices:
            extra["filter_choices"] = [c for c in filter_choices if isinstance(c, dict)][:20]
        return await self._run(
            question=question,
            session_id=session_id,
            user_context=user_context,
            limit=limit,
            temperature=temperature,
            eval_analytics=eval_analytics,
            llm_timeout=llm_timeout,
            progress_callback=progress_callback,
            partial_callback=partial_callback,
            answer_callback=answer_callback,
            analysis=extra or None,
        )

    async def process_confirmed_analysis(
        self,
        *,
        question: str,
        skill: str,
        params: Dict[str, Any],
        session_id: Optional[UUID],
        user_context: Optional[Dict[str, Any]] = None,
        parent_query_id: Optional[UUID] = None,
        override_guards: bool = False,
        confirmed: bool = True,
        eval_analytics: Optional[bool] = None,
        llm_timeout: Optional[int] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        """Re-enter the graph with server-validated ML parameters.

        Used by ``/api/analysis/run`` and ``/rerun``: the planner is skipped
        (``analysis_resume``) and the result is a new child turn of
        ``parent_query_id`` in the same conversation. ``confirmed=False`` (a
        resolved clarification or guard exit) still stops at the first-run
        confirm card unless the skill is remembered for this connection.
        """
        return await self._run(
            question=question,
            session_id=session_id,
            user_context=user_context,
            eval_analytics=eval_analytics,
            llm_timeout=llm_timeout,
            progress_callback=progress_callback,
            parent_query_id=parent_query_id,
            analysis={
                "analysis_skill": skill,
                "analysis_params": params,
                "analysis_resume": True,
                "analysis_confirmed": bool(confirmed),
                "analysis_override_guards": bool(override_guards),
                "route": "needs_analysis",
                "route_reason": "confirmed analysis" if confirmed else "resumed analysis",
                "route_source": "confirmed_resume",
            },
        )

    async def _run(
        self,
        *,
        question: str,
        session_id: Optional[UUID],
        user_context: Optional[Dict[str, Any]],
        limit: Optional[int] = None,
        temperature: Optional[float] = None,
        eval_analytics: Optional[bool],
        llm_timeout: Optional[int],
        progress_callback: Optional[ProgressCallback],
        parent_query_id: Optional[UUID] = None,
        analysis: Optional[Dict[str, Any]] = None,
        partial_callback: Optional[PartialCallback] = None,
        answer_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        if not session_id:
            session_id = uuid4()

        request_started = time.monotonic()
        pre_graph_open = True
        emit_pre_graph(progress_callback, "node_started")
        value_prefetch: Optional[ValueLookupPrefetch] = None
        # Trace length when the answer went out; later steps ran after it.
        answered_at: Optional[int] = None

        def emit_answer(state: AgentState) -> None:
            nonlocal answered_at
            if answer_callback is None or answered_at is not None:
                return
            formatted = dict(state.get("formatted_response") or {})
            # Copies: enrichment writes onto the events, and the state's own
            # events are persisted (slimmed) and enriched after the graph.
            trace = [dict(event) for event in state.get("trace") or []]
            if trace:
                _enrich_trace(trace, state)
                formatted["trace"] = trace
            formatted["saving"] = True
            answer_callback(formatted)
            answered_at = len(trace)

        try:
            user = await self.user_resolver.resolve_user(user_context or {})

            # Live-editable global guardrails (DB statement timeout, row cap,
            # conversation-context window). Cached briefly inside the service.
            from src.metadata.runtime_settings import get_runtime_settings
            runtime = await get_runtime_settings()
            # The filter planner's value lookup runs next to the catalog
            # pre-load below instead of after it.
            try:
                value_prefetch = self._start_value_prefetch(question, runtime)
            except Exception:  # noqa: BLE001 - an optimisation must never fail the question
                logger.debug("value lookup prefetch not started", exc_info=True)

            # ── Parallel DB round-trips ──────────────────────────────────────
            # metadata_loader, conversation history, and query audit log are
            # all independent — run them concurrently to save ~1-2s of Azure
            # network latency on every request.
            #
            # log_query is non-fatal: if the audit insert fails (e.g. DB
            # hiccup) the flow continues with query_id=None and the error is
            # surfaced in the UI via formatted_response["error"].
            # Remembered filter-grounding choices: persist the answers this
            # request carries, then load the user's full set for the grounder.
            from src.metadata.filter_preferences import get_filter_preference_store
            preference_store = get_filter_preference_store()
            incoming_choices = list((analysis or {}).get("filter_choices") or [])
            if incoming_choices:
                try:
                    await preference_store.save(str(user.id), self.source_key, incoming_choices)
                except Exception as exc:  # noqa: BLE001 - memory must never fail a question
                    logger.info("filter preference save skipped (%s)", type(exc).__name__)

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
                    parent_query_id=parent_query_id,
                ),
                preference_store.load(str(user.id), self.source_key),
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
            filter_preferences: List[Dict[str, Any]] = (
                results[3] if not isinstance(results[3], Exception) else []
            )
            pre_graph_ms = int((time.monotonic() - request_started) * 1000)
            emit_pre_graph(progress_callback, "node_finished", elapsed_ms=pre_graph_ms)
            pre_graph_open = False
            mcp_timing = catalog_meta.get("mcp_timing") if isinstance(catalog_meta, dict) else None
            if isinstance(mcp_timing, dict):
                pre_graph_detail = (
                    f"question-specific catalog {int(mcp_timing.get('filtered_tool_ms') or 0)}ms"
                    f" · reusable catalog {int(mcp_timing.get('full_restore_ms') or 0)}ms"
                    f" · connection lookup {int(mcp_timing.get('connection_ms') or 0)}ms"
                    f" · pre-graph wall {pre_graph_ms}ms (parallel total)"
                )
            else:
                pre_graph_detail = (
                    f"catalog/history/audit pre-load in parallel"
                    f" · catalog {int(catalog_meta.get('load_ms') or 0)}ms"
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
                "partial_callback": partial_callback,
                "partial_revision": 0,
                "answer_callback": emit_answer,
                # ── Connection ──────────────────────────────────────────
                "connection_display_name": self.display_name,
                "database_type": self.database_type,
                "connection_database": self.connection.connection_database,
                "connection_catalog": self.connection.connection_catalog,
                "connection_schema": self.connection.db_schema,
                # ── Audit ───────────────────────────────────────────────
                "query_id": query_id,
                "user_id": str(user.id),
                # Include user resolution, runtime settings and the parallel
                # catalog/history/audit pre-load in total backend time.
                "start_time": request_started,
                "llm_call_count": 0,
                "llm_latency_ms": 0,
                "token_usage": {},
                # ── Memory ──────────────────────────────────────────────
                "conversation_history": conversation_context,
                "memory_window": runtime.conversation_context_turns,
                "prior_refs": [],
                "history_query": None,
                # ── Routing ─────────────────────────────────────────────
                "route": "needs_query",
                "route_reason": "",
                "route_source": None,
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
                "filter_lookup_timeout_ms": runtime.sql_filter_lookup_timeout_ms,
                "filter_cache_ttl_seconds": runtime.sql_filter_cache_ttl_seconds,
                "filter_metadata_evidence_enabled": runtime.sql_filter_metadata_evidence_enabled,
                "filter_value_visibility": runtime.sql_filter_value_visibility,
                "filter_unverified_execution": runtime.sql_filter_unverified_execution,
                "filter_source_probe_enabled": runtime.sql_filter_source_probe_enabled,
                "filter_source_distinct_enabled": runtime.sql_filter_source_distinct_enabled,
                "filter_probe_denylist": runtime.sql_filter_probe_denylist,
                "filter_existence_max_age_hours": runtime.sql_filter_existence_max_age_hours,
                "filter_absence_max_age_hours": runtime.sql_filter_absence_max_age_hours,
                "filter_candidates": [],
                "filter_value_search": None,
                "filter_value_prefetch": value_prefetch,
                "filter_choices": [],
                "filter_preferences": filter_preferences,
                "filter_clarification": None,
                "plan_assumptions": [],
                "filter_metrics": None,
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
                # ── Empty-result recheck ────────────────────────────────
                "empty_result_diagnostics": 0,
                "needs_sql_recheck": False,
                "empty_recheck_context": None,
                "empty_hint": None,
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
                # The LangGraph node timers begin after the pre-load above. Keep
                # that cold-start cost visible so the trace reconciles with wall
                # time instead of making a 10s MCP catalog fetch disappear.
                "trace": [{
                    "node": "pre_graph_setup",
                    "elapsed_ms": pre_graph_ms,
                    "icon": "⏱",
                    "type": "db",
                    "catalog_source": catalog_meta.get("source", "db"),
                    "detail": pre_graph_detail,
                    "mcp_timing": mcp_timing if isinstance(mcp_timing, dict) else None,
                }],
                # Empty dict — each LLM node adds its rendered prompt here
                "node_prompts": {},
                # ── ML skills ─────────────────────────────────────────────────
                "analysis_enabled_override": None,
                "analysis_skill": None,
                "analysis_params": None,
                "analysis_resume": False,
                "analysis_confirmed": False,
                "analysis_confirm_required": False,
                "analysis_override_guards": False,
                "analysis_proposal": None,
                "analysis_clarification": None,
                "analysis_guard_failure": None,
                "analysis_guard_results": [],
                "analysis_dropped_filters": [],
                "analysis_span": None,
                "analysis_result": None,
                "analysis_definition": None,
                "analysis_error": None,
                "low_confidence": False,
                "parent_query_id": parent_query_id,
                # ── Output ─────────────────────────────────────────────────────────────────
                "answer": None,
                # Surface pre-graph errors (e.g. audit log failure) in the UI
                # response without stopping the query flow.
                "error": pre_graph_error,
            }
            if analysis:
                initial_state.update(analysis)

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
                if answered_at is not None:
                    for event in raw_trace[answered_at:]:
                        event["after_answer"] = True
                formatted["trace"] = raw_trace

            return formatted

        except Exception as e:  # noqa: BLE001
            if pre_graph_open:
                emit_pre_graph(
                    progress_callback, "node_failed",
                    elapsed_ms=int((time.monotonic() - request_started) * 1000),
                )
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
        finally:
            # Routes that never reach the planner leave it running.
            if value_prefetch is not None:
                value_prefetch.cancel()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _start_value_prefetch(self, question: str, runtime: Any) -> Optional[ValueLookupPrefetch]:
        """Start the planner's reverse lookup now, for an MCP catalog only.

        That is where the value search is a slow provider call; a metadata-DB
        lookup is local SQL and runs in the planner as before. Everything
        async (the catalog source, the full catalog's schema words) resolves
        inside the background task, so the pre-load does not wait on it.
        """
        if not runtime.sql_filter_metadata_evidence_enabled or runtime.sql_filter_value_visibility == "none":
            return None
        from src.api import state as app_state  # noqa: PLC0415
        from src.agent.langgraph_agent.value_store_provider import value_store_for  # noqa: PLC0415

        service = getattr(app_state, "mcp_server_service", None)
        client = getattr(app_state, "mcp_catalog_client", None)
        if service is None or client is None:
            return None

        async def resolve_store():
            if await service.get_catalog_source(self.source_key) != "mcp":
                return None
            return "mcp", value_store_for({
                "catalog_source_used": "mcp",
                "filter_metadata_evidence_enabled": runtime.sql_filter_metadata_evidence_enabled,
            })

        async def load_catalog():
            bundle = await client.load_all(self.source_key)
            table_columns, _ = _extract_columns(bundle.get("columns", ""))
            return bundle, table_columns

        return start_value_lookup_prefetch(
            question, self.source_key, resolve_store=resolve_store, load_catalog=load_catalog,
        )

    async def _safe_log_query(
        self,
        *,
        user_id: Any,
        session_id: UUID,
        question: str,
        parent_query_id: Optional[UUID] = None,
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
            parent_query_id=parent_query_id,
            source_label=self.display_name,
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
                session_id=session_id,
                user_id=user_id,
                limit=limit,
                source_key=self.source_key,
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
        analysis_store: Any = None,
        analysis_runner_provider: Any = None,
        analysis_limiter: Any = None,
        analysis_audit: Any = None,
        usage_ledger: Any = None,
    ):
        self.llm = llm_service
        self.router_llm = router_llm_service or llm_service
        self.metadata_loader = metadata_loader
        self.connection_service = connection_service
        self.history = history_service
        self.user_resolver = user_resolver
        # ML skills collaborators (shared across per-connection agents).
        self.analysis_store = analysis_store
        self.analysis_runner_provider = analysis_runner_provider
        self.analysis_limiter = analysis_limiter
        self.analysis_audit = analysis_audit
        # Durable usage ledger for the admin Analytics page (may be None).
        self.usage_ledger = usage_ledger
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
                analysis_store=self.analysis_store,
                analysis_runner_provider=self.analysis_runner_provider,
                analysis_limiter=self.analysis_limiter,
                analysis_audit=self.analysis_audit,
                usage_ledger=self.usage_ledger,
            )
            self._agents[source_key] = agent
            logger.info("✅ Built JeenInsightsAgent for source_key=%s", source_key)
            return agent

    async def close(self) -> None:
        await self.connection_service.close()
        self._agents.clear()
