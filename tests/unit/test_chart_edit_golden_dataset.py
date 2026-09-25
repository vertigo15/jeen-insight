from __future__ import annotations

from pathlib import Path

import yaml


DATASET = (
    Path(__file__).resolve().parents[2]
    / "evals"
    / "datasets"
    / "chart_edit_operations.yaml"
)

ALLOWED_OPS = {
    "set_color",
    "set_style",
    "set_toggle",
    "set_format",
    "rename_series",
    "hide_series",
    "add_overlay",
    "remove_overlay",
    "set_sort",
    "set_chart_type",
    "set_stack",
    "set_binding",
    "set_palette",
    "scenario_set_point",
    "scenario_scale",
    "scenario_shift",
    "scenario_clear",
    "add_reference_line",
    "remove_reference_line",
    "highlight_points",
    "clear_highlights",
}
ML_FORBIDDEN = {
    "set_sort",
    "set_chart_type",
    "set_stack",
    "set_binding",
    "set_palette",
    "scenario_set_point",
    "scenario_scale",
    "scenario_shift",
    "scenario_clear",
}


def test_chart_edit_golden_set_is_large_unique_and_well_formed():
    payload = yaml.safe_load(DATASET.read_text(encoding="utf-8"))
    cases = payload["cases"]

    assert payload["version"] == 1
    assert len(cases) >= 50
    assert len({case["id"] for case in cases}) == len(cases)
    assert {"sql", "ml_band"} <= {case["kind"] for case in cases}
    assert any(any("\u0590" <= char <= "\u05ff" for char in case["instruction"]) for case in cases)

    for case in cases:
        assert case["id"] and case["instruction"]
        assert bool(case.get("ops")) != bool(case.get("out_of_scope"))
        assert set(case.get("ops") or []) <= ALLOWED_OPS
        if case["kind"].startswith("ml") and case.get("ops"):
            assert not set(case["ops"]).intersection(ML_FORBIDDEN)
        if case.get("out_of_scope"):
            assert case.get("reason") in {"needs_new_query", "ml_locked", "ambiguous"}

