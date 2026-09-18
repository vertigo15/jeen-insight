"""Unit tests for the DAX graph: it compiles, and its routing functions branch
to the right nodes. Collaborators are mocked; only wiring is under test.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.agent.langgraph_agent_dax.graph import (
    _make_route_from_trivial,
    _route_from_catalog,
    _route_from_entity_resolver,
    _route_from_eval,
    _route_from_execute,
    _route_from_feedback,
    _route_from_generator,
    _route_from_integrity,
    _route_from_memory_answer,
    _route_from_planner,
    _route_from_router,
    _route_from_validate,
    build_dax_graph,
)
from src.agent.langgraph_agent_dax.prompt_loader import DaxPromptLoader


class TestCompiles:
    def test_build_dax_graph_compiles(self):
        graph = build_dax_graph(
            llm=MagicMock(),
            router_llm=MagicMock(),
            metadata_loader=MagicMock(),
            history_service=MagicMock(),
            prompt_loader=DaxPromptLoader(),
            deployment_name="gpt-x",
            max_retries=4,
        )
        assert graph is not None
        assert hasattr(graph, "ainvoke")


class TestDaxCapabilityPrompt:
    def test_dax_loader_serves_power_bi_capability_prompt(self):
        # prompts_dax/capability_answer.md must shadow the SQL/ML-skill version:
        # a Power BI user asking "what can you do?" should not be told about
        # SELECT statements or anomaly-detection skills.
        rendered = DaxPromptLoader().render(
            "capability_answer",
            question="what can you do?",
            connection_display_name="Sales PBI",
            skill_catalog="- **Anomaly detection** …",
        )
        assert "text-to-DAX" in rendered
        assert "Sales PBI" in rendered
        assert "Anomaly detection" not in rendered
        assert "text-to-SQL" not in rendered

    async def test_db_prompt_cache_does_not_shadow_dax_owned_templates(self):
        # In production the DAX agent shares the SQL agent's PromptCache, whose
        # `capability_answer` row is seeded from prompts/capability_answer.md.
        # DAX-owned names must bypass it; shared names must still use it.
        class _Cache:
            async def get_content(self, name):
                return f"DB VERSION OF {name}"

            async def get_model_override(self, name):
                return "db-model"

        loader = DaxPromptLoader()
        loader.attach_cache(_Cache())

        cap = await loader.arender(
            "capability_answer", question="q", connection_display_name="PBI", skill_catalog=""
        )
        assert "text-to-DAX" in cap and "DB VERSION" not in cap
        assert await loader.model_override_for("capability_answer") is None

        assert await loader.arender("fused_router") == "DB VERSION OF fused_router"
        assert await loader.model_override_for("fused_router") == "db-model"


class TestRouter:
    def test_from_memory(self):
        assert _route_from_router({"route": "from_memory"}) == "memory_answer_generator"

    def test_greeting_short_circuits(self):
        assert _route_from_router({"route": "greeting"}) == "response_formatter"

    def test_history_lookup(self):
        assert _route_from_router({"route": "history_lookup"}) == "history_search"

    def test_capability_gets_its_own_node(self):
        # Before this route existed on the DAX graph, "what can you do?" fell
        # through to the catalog and was answered with a DAX query.
        assert _route_from_router({"route": "capability"}) == "capability_answer"

    def test_clarify_route_is_terminal(self):
        assert _route_from_router({"route": "clarify_route"}) == "response_formatter"

    def test_needs_query_to_catalog(self):
        assert _route_from_router({"route": "needs_query"}) == "dax_catalog_lookup"

    def test_memory_answer_needs_query(self):
        assert _route_from_memory_answer({"route": "needs_query"}) == "dax_catalog_lookup"
        assert _route_from_memory_answer({}) == "response_formatter"


class TestQueryCoreRouting:
    def test_catalog_blocked_to_formatter(self):
        assert _route_from_catalog({"catalog_blocked": True}) == "response_formatter"
        assert _route_from_catalog({}) == "dax_query_planner"

    def test_planner_clarification(self):
        assert _route_from_planner({"clarification_required": True}) == "response_formatter"
        assert _route_from_planner({}) == "dax_entity_resolver"

    def test_entity_resolver_clarification(self):
        assert (
            _route_from_entity_resolver({"clarification_required": True})
            == "response_formatter"
        )
        assert _route_from_entity_resolver({}) == "dax_prompt_builder"

    def test_generator(self):
        assert _route_from_generator({"generated_dax": "EVALUATE X"}) == "dax_static_validate"
        assert _route_from_generator({}) == "response_formatter"

    def test_validate_blocking(self):
        assert _route_from_validate({"dax_validation_error": "x"}) == "response_formatter"
        assert _route_from_validate({"dlp_blocked": True}) == "response_formatter"

    def test_validate_repairable(self):
        assert _route_from_validate({"dax_repairable_error": "x"}) == "dax_repair"

    def test_validate_ok_executes(self):
        assert _route_from_validate({}) == "pbi_execute_query"

    def test_execute_terminal(self):
        assert _route_from_execute({"dax_terminal": True}) == "response_formatter"

    def test_execute_error_to_feedback(self):
        assert _route_from_execute({"exec_error": "boom"}) == "dax_feedback_router"

    def test_execute_ok_to_integrity(self):
        assert _route_from_execute({}) == "result_integrity_check"

    def test_integrity_empty_diagnostic(self):
        assert _route_from_integrity({"integrity_action": "empty_diagnostic"}) == "dax_feedback_router"
        assert _route_from_integrity({}) == "trivial_result_check"


class TestTerminalRouting:
    def test_trivial_skips_eval_when_disabled(self):
        route = _make_route_from_trivial(eval_enabled=False)
        assert route({}) == "response_formatter"

    def test_trivial_goes_to_eval_when_enabled(self):
        route = _make_route_from_trivial(eval_enabled=True)
        assert route({}) == "fused_eval_analytics"

    def test_trivial_result_skips_eval(self):
        route = _make_route_from_trivial(eval_enabled=True)
        assert route({"is_trivial": True}) == "response_formatter"

    def test_eval_semantic_mismatch_to_feedback(self):
        assert _route_from_eval({"eval_result": {"answers_intent": False}}) == "dax_feedback_router"
        assert _route_from_eval({"eval_result": {"answers_intent": True}}) == "response_formatter"

    def test_feedback_actions(self):
        assert _route_from_feedback({"dax_feedback_action": "local_repair"}) == "dax_repair"
        assert _route_from_feedback({"dax_feedback_action": "regenerate"}) == "dax_generator"
        assert _route_from_feedback({"dax_feedback_action": "replan"}) == "dax_query_planner"
        assert (
            _route_from_feedback({"dax_feedback_action": "resolve_entities"})
            == "dax_entity_resolver"
        )
        assert _route_from_feedback({"dax_feedback_action": "refresh_catalog"}) == "dax_catalog_lookup"
        assert _route_from_feedback({"dax_feedback_action": "exhausted"}) == "response_formatter"
