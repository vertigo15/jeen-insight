"""The measurable slice of a result envelope.

One flat dict per completed analysis — what the ``analysis_result`` log event
carries and what the forecast backtest aggregates — so "how often does the
baseline win", "how deep is the CV", "does the band cover" are questions a log
query can answer before any accuracy change is judged.

Pure over the envelope (model or its ``model_dump``); no ML dependency.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def result_metrics(envelope: Any) -> Dict[str, Any]:
    """Skill / method / validation / CV depth / guard signals, all scalar."""
    facts: Dict[str, Any] = _get(envelope, "facts") or {}
    validation = _get(envelope, "validation") or {}
    params: Dict[str, Any] = _get(envelope, "params") or {}
    series = params.get("series") if isinstance(params, dict) else None
    guards = _get(envelope, "guard_results") or []
    method = _get(envelope, "method_used")
    baseline = facts.get("baseline")

    def guard(name: str) -> Optional[Dict[str, Any]]:
        for g in guards:
            if _get(g, "name") == name:
                return {"passed": bool(_get(g, "passed")), "observed": _get(g, "observed"),
                        "required": _get(g, "required")}
        return None

    out: Dict[str, Any] = {
        "skill": _get(envelope, "skill"),
        "method": method,
        "metric": _get(validation, "metric"),
        "metric_value": _get(validation, "value"),
        "band": _get(validation, "band"),
        "coverage": _get(validation, "coverage"),
        "coverage_n": _get(validation, "coverage_n"),
        "low_confidence": bool(_get(envelope, "low_confidence")),
        "grain": facts.get("grain") or (series.get("grain") if isinstance(series, dict) else None),
        "n_points": facts.get("n_points"),
        "seasonal_periods": facts.get("seasonal_periods"),
        "engine_hash": _get(_get(envelope, "engine"), "module_hash"),
        "overridden_guards": [_get(g, "name") for g in guards if not _get(g, "passed")],
    }
    if out["skill"] == "forecast":
        out.update({
            "horizon": facts.get("horizon"),
            "interval": facts.get("interval"),
            "interval_method": facts.get("interval_method"),
            "cv_windows": facts.get("cv_windows"),
            "baseline": baseline,
            "baseline_won": bool(baseline) and method == baseline,
            "floored_at_zero": facts.get("floored_at_zero"),
            "intermittent": guard("intermittent"),
        })
    return out
