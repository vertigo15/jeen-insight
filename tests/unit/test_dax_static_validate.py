"""Unit tests for dax_static_validate (symbol resolution + DLP + TOPN gate).

The validator classifies failures into a BLOCKING channel (``dax_validation_error``
/ ``dlp_blocked``) vs a REPAIRABLE channel (``dax_repairable_error`` +
``dax_lint_errors``). These tests pin that split and the symbol-resolution rules
(``'Table'[Column]`` vs ``[Measure]``).
"""

from __future__ import annotations

from src.agent.langgraph_agent_dax.nodes.dax_validate import make_dax_static_validate


def _catalog_state(**overrides):
    state = {
        "known_tables": ["Sales", "Customer"],
        "table_columns": {"sales": ["amount", "region"], "customer": ["salary"]},
        "known_columns": ["amount", "region", "salary"],
        "known_measures": ["total sales"],
        "plan_grain": "aggregate",
        "repair_attempts_by_category": {},
    }
    state.update(overrides)
    return state


def _validate(dax, *, governed=None, **overrides):
    node = make_dax_static_validate(
        enabled=True, require_catalog=True, dlp_enabled=True, dlp_governed_columns=governed
    )
    st = _catalog_state(**overrides)
    st["generated_dax"] = dax
    return node(st)


class TestPass:
    def test_known_symbols_pass(self):
        out = _validate("EVALUATE FILTER(Sales, 'Sales'[Amount] > 0)")
        assert out["dax_validation_error"] is None
        assert out["dax_repairable_error"] is None
        assert out["dlp_blocked"] is False

    def test_known_measure_passes(self):
        out = _validate('EVALUATE ROW("v", [Total Sales])')
        assert out["dax_repairable_error"] is None
        assert out["dax_validation_error"] is None


class TestRepairable:
    def test_unknown_column_is_repairable(self):
        out = _validate("EVALUATE FILTER(Sales, 'Sales'[Bogus] > 0)")
        assert out["dax_repairable_error"]
        assert out["dax_validation_error"] is None

    def test_unknown_table_is_repairable(self):
        out = _validate("EVALUATE 'Ghost'[Amount]")
        assert out["dax_repairable_error"]

    def test_unknown_measure_is_repairable(self):
        out = _validate('EVALUATE ROW("v", [Nonexistent Measure])')
        assert out["dax_repairable_error"]

    def test_detail_grain_requires_topn(self):
        out = _validate("EVALUATE Sales", plan_grain="detail")
        assert out["dax_repairable_error"]
        assert any("TOPN" in e for e in out["dax_lint_errors"])

    def test_detail_grain_with_topn_passes(self):
        out = _validate("EVALUATE TOPN(10, Sales)", plan_grain="detail")
        assert out["dax_repairable_error"] is None

    def test_promotes_to_blocking_when_repair_budget_spent(self):
        out = _validate(
            "EVALUATE 'Ghost'[Amount]",
            repair_attempts_by_category={"static": 2},
        )
        assert out["dax_validation_error"]
        assert out["dax_repairable_error"] is None


class TestBlocking:
    def test_banned_token_blocks(self):
        out = _validate('EVALUATE ROW("v", 1) DROP TABLE Sales')
        assert out["dax_validation_error"]
        assert out["dax_repairable_error"] is None

    def test_dlp_blocks_governed_column(self):
        out = _validate("EVALUATE 'Customer'[salary]", governed=["salary"])
        assert out["dlp_blocked"] is True
        assert out["governance_error"]
        assert out["governed_lineage"]

    def test_missing_catalog_blocks(self):
        node = make_dax_static_validate(enabled=True, require_catalog=True)
        out = node({"generated_dax": "EVALUATE Sales", "known_tables": []})
        assert out["dax_validation_error"]


class TestDisabled:
    def test_disabled_is_noop(self):
        node = make_dax_static_validate(enabled=False)
        out = node({"generated_dax": "EVALUATE ROW(\"v\",1) DROP"})
        assert out["dax_validation_error"] is None
        assert out["dax_repairable_error"] is None
