"""Jeen Insights agent: text-to-SQL orchestrator.

The agent fetches curated metadata from the shared metadata DB at the start
of every question (via `MetadataLoader`), substitutes it into the system
prompt, calls the LLM, executes the resulting SQL against the user-selected
data source, and writes a full lifecycle record to `insights_*` tables.

`AgentRegistry` lazily builds one `JeenInsightsAgent` per `source_key` and
shares the heavy collaborators (LLM service, metadata pool, history service,
connection service) across them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from src.agent.conversation_history import ConversationHistoryService
from src.agent.llm_service import LangChainLlmService
from src.agent.prompt_cache import PromptCache
from src.agent.user_resolver import SimpleUserResolver
from src.connections import Connection, ConnectionService
from src.metadata import MetadataLoader
from src.tools.sql_tool import PostgresSqlRunner, RunSqlTool

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Agent
# ----------------------------------------------------------------------
class JeenInsightsAgent:
    """Per-connection text-to-SQL agent."""

    def __init__(
        self,
        *,
        connection: Connection,
        sql_runner: PostgresSqlRunner,
        llm_service: LangChainLlmService,
        prompt_cache: PromptCache,
        metadata_loader: MetadataLoader,
        history_service: ConversationHistoryService,
        user_resolver: SimpleUserResolver,
    ):
        self.connection = connection
        self.source_key = connection.source_key
        self.display_name = connection.display_name
        self.database_type = connection.database_type
        self.sql_runner = sql_runner
        self.llm = llm_service
        self.prompt_cache = prompt_cache
        self.metadata_loader = metadata_loader
        self.history = history_service
        self.user_resolver = user_resolver
        self.sql_tool = RunSqlTool(
            sql_runner,
            connection_display_name=connection.display_name,
            database_type=connection.database_type,
        )

    async def process_question(
        self,
        *,
        question: str,
        session_id: Optional[UUID] = None,
        user_context: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Run the text-to-SQL pipeline.

        ``limit`` and ``temperature`` are optional per-request overrides
        sourced from the user's settings panel. ``None`` means "use the
        server's default". Server-side bounds are enforced by the Pydantic
        request schema.
        """
        if not session_id:
            session_id = uuid4()

        query_id: Optional[UUID] = None
        llm_latency_ms: Optional[int] = None

        try:
            user = await self.user_resolver.resolve_user(user_context or {})

            metadata_bundle = await self.metadata_loader.load_all(self.source_key)
            conversation_context = await self._fetch_conversation_context(session_id)

            query_id = await self.history.log_query(
                user_id=user.id,
                source_key=self.source_key,
                session_id=session_id,
                natural_language_query=question,
                dataset_id=self.source_key,
                rag_context=self._summarize_metadata(metadata_bundle),
            )

            # Load system prompt and optional per-prompt model override from
            # cache (DB on first access, in-memory on subsequent calls).
            prompt_template = await self.prompt_cache.get_content("jeen_insights_system")
            model_override = await self.prompt_cache.get_model_override("jeen_insights_system")
            system_prompt = self._build_system_prompt(prompt_template, metadata_bundle)

            messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
            for prev_qa in conversation_context:
                if prev_qa.get("natural_language_query") and prev_qa.get("generated_sql"):
                    messages.append(
                        {
                            "role": "user",
                            "content": prev_qa["natural_language_query"],
                        }
                    )
                    messages.append(
                        {
                            "role": "assistant",
                            "content": prev_qa["generated_sql"],
                        }
                    )
            messages.append({"role": "user", "content": question})

            tools = [self.sql_tool.get_schema()]

            structured_prompt = {
                "tables": metadata_bundle.get("tables", ""),
                "columns": metadata_bundle.get("columns", ""),
                "relationships": metadata_bundle.get("relationships", ""),
                "sources": metadata_bundle.get("sources", ""),
                "knowledge_pairs": metadata_bundle.get("knowledge_pairs", ""),
                "business_terms": metadata_bundle.get("business_terms", ""),
                "tool_description": tools[0] if tools else None,
                "conversation_history": [
                    {
                        "question": prev.get("natural_language_query"),
                        "sql": prev.get("generated_sql"),
                    }
                    for prev in conversation_context
                    if prev.get("natural_language_query") and prev.get("generated_sql")
                ],
                "current_question": question,
                "full_text": system_prompt,
                "connection": {
                    "source_key": self.source_key,
                    "display_name": self.display_name,
                    "database_type": self.database_type,
                },
            }

            llm_start = time.time()
            # Per-request temperature override (clamped 0.0–1.0 by the request
            # schema). Falls back to the centralised QUERY_PARAMS default.
            from src.api.llm_params import QUERY_PARAMS

            effective_temperature = (
                temperature if temperature is not None else QUERY_PARAMS.temperature
            )
            response = await self.llm.generate(
                messages=messages,
                temperature=effective_temperature,
                max_tokens=QUERY_PARAMS.max_tokens,
                tools=tools,
                model_override=model_override,
            )
            llm_latency_ms = int((time.time() - llm_start) * 1000)

            usage = response.get("usage") or {}
            result: Dict[str, Any] = {
                "question": question,
                "query_id": query_id,
                "session_id": session_id,
                "sql": None,
                "results": None,
                "prompt": structured_prompt,
                "error": None,
                "metrics": {
                    "input_tokens": usage.get("prompt_tokens"),
                    "output_tokens": usage.get("completion_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                    "llm_latency_ms": llm_latency_ms,
                },
            }

            sql = self._extract_sql(response)
            if sql:
                result["sql"] = sql
                await self.history.update_llm_response(
                    query_id=query_id,
                    generated_sql=sql,
                    llm_model=self.llm.get_deployment(),
                    llm_latency_ms=llm_latency_ms or 0,
                    tokens_used=response.get("usage", {}).get("total_tokens", 0),
                )

                exec_start = time.time()
                # Per-request row cap; sql_runner falls back to its built-in
                # default (100) when None is passed.
                query_result = await self.sql_runner.run_sql(
                    sql, limit=limit if limit is not None else 100
                )
                exec_time_ms = int((time.time() - exec_start) * 1000)
                result["results"] = query_result

                if "error" in query_result:
                    await self.history.update_execution(
                        query_id=query_id,
                        execution_status="error",
                        execution_time_ms=exec_time_ms,
                        row_count=0,
                        result_preview=None,
                        error_message=query_result["error"],
                    )
                    result["error"] = query_result["error"]
                else:
                    rows = query_result.get("rows", [])
                    await self.history.update_execution(
                        query_id=query_id,
                        execution_status="success",
                        execution_time_ms=exec_time_ms,
                        row_count=len(rows),
                        result_preview=rows[:10] if rows else None,
                        error_message=None,
                    )

            return result

        except Exception as e:  # noqa: BLE001
            logger.exception("Error processing question")
            if query_id:
                try:
                    await self.history.update_execution(
                        query_id=query_id,
                        execution_status="error",
                        execution_time_ms=None,
                        row_count=0,
                        result_preview=None,
                        error_message=str(e),
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to log error to history")
            return {
                "question": question,
                "query_id": query_id,
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
    async def _fetch_conversation_context(self, session_id: UUID) -> List[Dict[str, Any]]:
        try:
            ctx = await self.history.get_conversation_context(
                session_id=session_id, limit=2
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

    def _build_system_prompt(self, template: str, metadata_bundle: Dict[str, str]) -> str:
        """Render the system prompt template with live metadata."""
        return template.format(
            connection_display_name=self.display_name,
            database_type=self.database_type,
            tables=metadata_bundle.get("tables", ""),
            columns=metadata_bundle.get("columns", ""),
            relationships=metadata_bundle.get("relationships", ""),
            sources=metadata_bundle.get("sources", ""),
            knowledge_pairs=metadata_bundle.get("knowledge_pairs", ""),
            business_terms=metadata_bundle.get("business_terms", ""),
        )

    def _summarize_metadata(self, bundle: Dict[str, str]) -> Dict[str, int]:
        return {
            key: len([line for line in value.splitlines() if line.startswith("- ")])
            for key, value in bundle.items()
        }

    def _extract_sql(self, response: Dict[str, Any]) -> Optional[str]:
        # Tool call path
        tool_calls = response.get("tool_calls") or []
        for tc in tool_calls:
            if tc.get("function", {}).get("name") == "run_sql":
                try:
                    args = json.loads(tc["function"]["arguments"])
                except (KeyError, json.JSONDecodeError):
                    continue
                sql = args.get("sql")
                if sql:
                    return sql

        # Fallback: parse SQL out of the assistant text
        text = response.get("content") or ""
        if not text:
            return None
        if "```sql" in text.lower():
            start = text.lower().find("```sql") + len("```sql")
            end = text.find("```", start)
            if end > start:
                return text[start:end].strip()
        if "SELECT" in text.upper():
            lines = text.splitlines()
            sql_lines: List[str] = []
            in_sql = False
            for line in lines:
                if "SELECT" in line.upper():
                    in_sql = True
                if in_sql:
                    sql_lines.append(line)
                    if ";" in line:
                        break
            return "\n".join(sql_lines).strip() or None
        return None


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------
class AgentRegistry:
    """Lazily builds one `JeenInsightsAgent` per `source_key`."""

    def __init__(
        self,
        *,
        llm_service: LangChainLlmService,
        prompt_cache: PromptCache,
        metadata_loader: MetadataLoader,
        connection_service: ConnectionService,
        history_service: ConversationHistoryService,
        user_resolver: SimpleUserResolver,
    ):
        self.llm = llm_service
        self.prompt_cache = prompt_cache
        self.metadata_loader = metadata_loader
        self.connection_service = connection_service
        self.history = history_service
        self.user_resolver = user_resolver
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
                prompt_cache=self.prompt_cache,
                metadata_loader=self.metadata_loader,
                history_service=self.history,
                user_resolver=self.user_resolver,
            )
            self._agents[source_key] = agent
            logger.info("✅ Built JeenInsightsAgent for source_key=%s", source_key)
            return agent

    async def close(self) -> None:
        await self.connection_service.close()
        self._agents.clear()
