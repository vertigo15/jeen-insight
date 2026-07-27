"""Unit tests for dax_feedback_router — DAX error taxonomy + split budgets.

Verifies each category routes to the right action and that the separate budgets
(1 local repair, 2 replans, 1 catalog refresh) downgrade correctly and never loop
past the overall retry ceiling.
"""

from __future__ import annotations

from src.agent.langgraph_agent_dax.nodes.feedback import make_dax_feedback_router

_ROUTER = make_dax_feedback_router(max_retries=4)


def _err_state(error_type, message, **overrides):
    st = {
        "retry_count": 0,
        "query_result": {"error": message, "error_type": error_type},
        "pbi_error_message": message,
        "exec_error": message,
        "repair_attempts_by_category": {},
        "plan_regenerations": 0,
        "catalog_refresh_count": 0,
    }
    st.update(overrides)
    return st


class TestTerminalCategories:
    def test_forbidden_is_terminal(self):
        out = _ROUTER(_err_state("forbidden", "denied"))
        assert out["dax_feedback_action"] == "exhausted"
        assert out["dax_error_category"] == "AUTHZ_OR_TENANT"

    def test_throttled_after_inline_retries_is_terminal(self):
        out = _ROUTER(_err_state("throttled", "429"))
        assert out["dax_feedback_action"] == "exhausted"

    def test_read_only_blocked_is_terminal(self):
        out = _ROUTER(_err_state("read_only_blocked", "not read-only"))
        assert out["dax_feedback_action"] == "exhausted"
        assert out["dax_error_category"] == "UNSAFE_OR_GOVERNED"


class TestRepairRouting:
    def test_lexical_default_goes_local_repair_first(self):
        out = _ROUTER(_err_state("execution_error", "Unexpected token near )"))
        assert out["dax_feedback_action"] == "local_repair"
        assert out["repair_attempts_by_category"]["local_repair"] == 1

    def test_lexical_second_time_regenerates(self):
        out = _ROUTER(_err_state(
            "execution_error", "Unexpected token",
            repair_attempts_by_category={"local_repair": 1},
        ))
        assert out["dax_feedback_action"] == "regenerate"

    def test_unknown_object_refreshes_catalog_once(self):
        out = _ROUTER(_err_state("execution_error", "Cannot find table 'Foo'"))
        assert out["dax_feedback_action"] == "refresh_catalog"
        assert out["catalog_refresh_count"] == 1

    def test_unknown_object_after_refresh_regenerates(self):
        out = _ROUTER(_err_state(
            "execution_error", "Cannot find column 'Bar'",
            catalog_refresh_count=1,
        ))
        assert out["dax_feedback_action"] == "regenerate"

    def test_relationship_replans(self):
        out = _ROUTER(_err_state("execution_error", "No relationship between tables"))
        assert out["dax_feedback_action"] == "replan"
        assert out["plan_regenerations"] == 1

    def test_relationship_after_two_replans_regenerates(self):
        out = _ROUTER(_err_state(
            "execution_error", "ambiguous relationship",
            plan_regenerations=2,
        ))
        assert out["dax_feedback_action"] == "regenerate"

    def test_time_semantics_replans(self):
        out = _ROUTER(_err_state("execution_error", "dates are not contiguous"))
        assert out["dax_feedback_action"] == "replan"


class TestSemanticAndEmpty:
    def test_empty_diagnostic_regenerates(self):
        st = {
            "retry_count": 0,
            "query_result": {"columns": [], "rows": [], "row_count": 0},
            "integrity_action": "empty_diagnostic",
            "error_context": "zero rows",
        }
        out = _ROUTER(st)
        assert out["dax_error_category"] == "EMPTY_OR_BLANK"
        assert out["dax_feedback_action"] == "regenerate"

    def test_semantic_mismatch_regenerates(self):
        st = {
            "retry_count": 0,
            "question": "top customers",
            "query_result": {"columns": ["x"], "rows": [{"x": 1}], "row_count": 1},
            "eval_result": {"answers_intent": False},
        }
        out = _ROUTER(st)
        assert out["dax_error_category"] == "SEMANTIC_MISMATCH"
        assert out["dax_feedback_action"] == "regenerate"


class TestBudgetCeiling:
    def test_exhausts_at_max_retries(self):
        out = _ROUTER(_err_state("execution_error", "Unexpected token", retry_count=4))
        assert out["dax_feedback_action"] == "exhausted"
