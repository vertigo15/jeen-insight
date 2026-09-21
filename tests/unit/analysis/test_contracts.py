"""Contract-level tests: every skill is registered, params validate with defaults,
patches are allowlisted, and the envelope's artifact view never carries rows."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.analysis.contracts import (
    AGG_LABELS,
    CONTRACT_VERSION,
    METHOD_LABELS,
    PARAMS_BY_SKILL,
    SKILLS,
    AnomalyParams,
    ChartSeriesSpec,
    ChartSpec,
    Egress,
    EngineInfo,
    ForecastParams,
    ModelDetails,
    ParamChip,
    Provenance,
    ResultEnvelope,
    SeriesRequest,
    Validation,
    field_bounds,
    get_skill,
    merge_params_patch,
    method_options,
    parse_params,
)


def _series(**over) -> dict:
    base = {"table": "FactInternetSales", "date_column": "OrderDate", "measure_column": "Profit"}
    base.update(over)
    return base


def test_every_registered_skill_has_a_params_model_and_engine():
    assert set(SKILLS) == set(PARAMS_BY_SKILL) == {
        "anomaly_detection", "forecast", "changepoint", "seasonality", "correlation",
        "contribution", "clustering", "driver_analysis", "regression", "classification",
        "cohort_retention", "experiment_test",
    }
    for spec in SKILLS.values():
        assert spec.engine.startswith("src.analysis.engines.")
        assert spec.routes_on and spec.guards
        assert spec.tier == ("B" if spec.family == "entity" else "A")
    tier_b = {name for name, s in SKILLS.items() if s.tier == "B"}
    assert tier_b == {"clustering", "driver_analysis", "regression", "classification"}


def test_params_defaults_match_spec():
    a = parse_params("anomaly_detection", {"series": _series()})
    assert isinstance(a, AnomalyParams)
    assert (a.sensitivity, a.method, a.window) == (0.95, "auto", None)
    assert a.series.grain == "week" and a.series.agg == "sum"
    assert a.series.measure_label == "SUM(Profit)"

    f = parse_params("forecast", {"series": _series(grain="month")})
    assert isinstance(f, ForecastParams)
    assert (f.horizon, f.interval, f.method, f.window) == (8, 0.80, "auto", None)


def test_forecast_window_is_a_real_bounded_patchable_parameter():
    # Contract 2: the forecast look-back is a field like the other series skills',
    # not a private planner hint — so the cards can show it and patches can change it.
    assert CONTRACT_VERSION == "2"
    assert parse_params("forecast", {"series": _series(), "window": 36}).window == 36
    for bad in (11, 1501, "lots"):
        with pytest.raises(ValidationError):
            parse_params("forecast", {"series": _series(), "window": bad})
    base = {"series": _series(grain="month"), "horizon": 8}
    assert merge_params_patch("forecast", base, {"window": 36}).window == 36
    assert merge_params_patch("forecast", {**base, "window": 24}, {"horizon": 12}).window == 24  # untouched by other patches


def test_method_options_come_from_the_params_model():
    # One source for the confirm-card chip and the result strip's model selector.
    assert method_options("forecast") == ["auto", "auto_arima", "auto_ets", "theta", "drift", "seasonal_naive"]
    assert method_options("anomaly_detection") == ["auto", "seasonal", "trend", "sigma3"]
    assert method_options("clustering") == ["kmeans", "hdbscan"]
    assert method_options("driver_analysis") == ["hgb", "xgboost", "lightgbm", "auto"]
    assert method_options("changepoint") == [] and method_options("experiment_test") == []
    assert method_options("no_such_skill") == []
    for option in method_options("forecast"):
        assert parse_params("forecast", {"series": _series(), "method": option}).method == option


def test_params_reject_out_of_range_and_unknown_fields():
    with pytest.raises(ValidationError):
        parse_params("anomaly_detection", {"series": _series(), "sensitivity": 0.5})
    with pytest.raises(ValidationError):
        parse_params("forecast", {"series": _series(), "horizon": 0})
    with pytest.raises(ValidationError):
        parse_params("forecast", {"series": _series(), "evil": 1})
    with pytest.raises(ValidationError):
        parse_params("anomaly_detection", {"series": _series(agg="median")})


def test_unknown_skill_is_refused():
    with pytest.raises(KeyError):
        get_skill("made_up_skill")


def test_tier_b_params_and_patches_use_the_entity_request():
    entity = {"table": "DimCustomer", "entity_key": "CustomerKey", "features": ["YearlyIncome", "TotalChildren"]}
    c = parse_params("clustering", {"entity": entity})
    assert c.k is None and c.entity.row_cap == 50_000
    merged = merge_params_patch("clustering", {"entity": entity}, {"k": 4, "row_cap": 1000, "grain": "week"})
    assert merged.k == 4 and merged.entity.row_cap == 1000  # 'grain' is not an entity field → dropped
    with pytest.raises(ValidationError):
        parse_params("driver_analysis", {"entity": entity})  # needs a target
    d = parse_params("driver_analysis", {"entity": {**entity, "target": "Profit"}})
    assert d.entity.target == "Profit" and d.holdout == 0.2 and d.method == "hgb"
    dm = parse_params("driver_analysis", {"entity": {**entity, "target": "Profit"}, "method": "auto"})
    assert dm.method == "auto"
    with pytest.raises(ValidationError):
        parse_params("driver_analysis", {"entity": {**entity, "target": "Profit"}, "method": "catboost"})
    with pytest.raises(ValidationError):
        parse_params("regression", {"entity": entity})  # needs a target
    r = parse_params("regression", {"entity": {**entity, "target": "Profit"}})
    assert r.entity.target == "Profit" and r.holdout == 0.2
    with pytest.raises(ValidationError):
        parse_params("regression", {"entity": {**entity, "target": "Profit"}, "holdout": 0.9})
    with pytest.raises(ValidationError):
        parse_params("classification", {"entity": entity})  # needs a target
    cl2 = parse_params("classification", {"entity": {**entity, "target": "Churn"}})
    assert cl2.entity.target == "Churn" and cl2.holdout == 0.2
    with pytest.raises(ValidationError):
        parse_params("contribution", {"series": _series(), "dimensions": []})
    k = parse_params("contribution", {"series": _series(), "dimensions": ["SalesTerritoryKey"]})
    assert k.top_n == 10


def test_cohort_retention_params_and_patches_use_the_cohort_request():
    cohort = {"table": "orders", "entity_key": "CustomerKey", "cohort_date": "SignupDate",
              "activity_date": "OrderDate"}
    p = parse_params("cohort_retention", {"cohort": cohort})
    assert p.cohort.grain == "month" and p.cohort.max_periods == 12
    # A patch reaches the nested cohort request, and the table cannot be re-pointed.
    merged = merge_params_patch("cohort_retention", {"cohort": cohort},
                                {"cohort": {"grain": "week", "max_periods": 6, "table": "evil"}})
    assert merged.cohort.grain == "week" and merged.cohort.max_periods == 6
    assert merged.cohort.table == "orders"  # table is patch-denied
    with pytest.raises(ValidationError):
        parse_params("cohort_retention", {"cohort": {**cohort, "max_periods": 0}})
    with pytest.raises(ValidationError):
        parse_params("cohort_retention", {"cohort": {**cohort, "grain": "year"}})
    with pytest.raises(ValidationError):
        parse_params("cohort_retention", {"cohort": {k: v for k, v in cohort.items() if k != "activity_date"}})


def test_experiment_test_params_and_patches_use_the_experiment_request():
    experiment = {"table": "events", "group_column": "variant", "outcome_column": "converted"}
    p = parse_params("experiment_test", {"experiment": experiment})
    assert p.experiment.outcome_type == "binary" and p.confidence == 0.95 and p.experiment.control is None
    # The control label and confidence are patchable; the table is not.
    merged = merge_params_patch("experiment_test", {"experiment": experiment},
                                {"experiment": {"control": "A", "table": "evil"}, "confidence": 0.99})
    assert merged.experiment.control == "A" and merged.confidence == 0.99
    assert merged.experiment.table == "events"  # table is patch-denied
    c = parse_params("experiment_test", {"experiment": {**experiment, "outcome_type": "continuous"}})
    assert c.experiment.outcome_type == "continuous"
    with pytest.raises(ValidationError):
        parse_params("experiment_test", {"experiment": {**experiment, "outcome_type": "ordinal"}})
    with pytest.raises(ValidationError):
        parse_params("experiment_test", {"experiment": experiment, "confidence": 0.5})
    with pytest.raises(ValidationError):
        parse_params("experiment_test", {"experiment": {k: v for k, v in experiment.items() if k != "outcome_column"}})


def test_merge_params_patch_is_allowlisted():
    base = {"series": _series(grain="day"), "sensitivity": 0.95}
    merged = merge_params_patch(
        "anomaly_detection",
        base,
        {"grain": "week", "sensitivity": 0.9, "series": {"agg": "avg"}, "__class__": "x", "rows": 1},
    )
    assert merged.series.grain == "week"
    assert merged.series.agg == "avg"
    assert merged.sensitivity == 0.9
    assert not hasattr(merged, "rows")
    # A patch that breaks validation still raises.
    with pytest.raises(ValidationError):
        merge_params_patch("anomaly_detection", base, {"sensitivity": 2})


def test_merge_params_patch_turns_the_cards_unset_words_back_into_none():
    # The chips show "split by: none" and "segments: auto"; picking them again
    # must clear the field, not name a column "none" or fail on int("auto").
    base = {"series": {**_series(), "group_by": "Territory"}}
    assert merge_params_patch("anomaly_detection", base, {"group_by": "none"}).series.group_by is None
    assert merge_params_patch("anomaly_detection", base, {"series": {"group_by": "None"}}).series.group_by is None
    assert merge_params_patch("anomaly_detection", base, {"group_by": "Region"}).series.group_by == "Region"
    entity = {"table": "DimCustomer", "entity_key": "CustomerKey", "features": ["YearlyIncome", "TotalChildren"]}
    assert merge_params_patch("clustering", {"entity": entity, "k": 4}, {"k": "auto"}).k is None
    assert merge_params_patch("clustering", {"entity": entity}, {"k": 3}).k == 3
    # "auto" stays a real value where it is one.
    assert merge_params_patch("forecast", {"series": _series()}, {"method": "auto"}).method == "auto"
    # A setup card persisted before the multiselect still sends features as one string.
    merged = merge_params_patch("clustering", {"entity": entity}, {"features": "YearlyIncome, Age, TotalChildren"})
    assert merged.entity.features == ["YearlyIncome", "Age", "TotalChildren"]
    assert merge_params_patch("clustering", {"entity": entity}, {"features": ["Age", "YearlyIncome"]}).entity.features == ["Age", "YearlyIncome"]


def test_field_bounds_read_the_contract_including_nested_request_models():
    # Top-level numeric fields: ge/le.
    assert field_bounds("forecast", "window") == (12, 1500)
    assert field_bounds("forecast", "horizon") == (1, 104)
    assert field_bounds("forecast", "interval") == (0.5, 0.99)
    assert field_bounds("seasonality", "window") == (24, 1500)
    # Nested request models: row_cap on EntityRequest, max_periods on CohortRequest.
    assert field_bounds("clustering", "row_cap") == (100, 200_000)
    assert field_bounds("cohort_retention", "max_periods") == (2, 36)
    # List fields report a count.
    assert field_bounds("clustering", "features") == (2, 8)
    assert field_bounds("contribution", "dimensions") == (1, 4)
    # Unbounded or unknown keys are simply unbounded.
    assert field_bounds("forecast", "measure_column") == (None, None)
    assert field_bounds("forecast", "no_such_field") == (None, None)


def test_param_chip_metadata_is_optional_and_labels_cover_every_enum():
    # Chips persisted before the metadata existed still parse and land in one section.
    chip = ParamChip(key="window", label="window", value=24)
    assert chip.group == "Setup" and chip.min is None and chip.option_labels == {}
    # Every selectable method of every skill has a human name.
    for skill in SKILLS:
        for option in method_options(skill):
            assert option in METHOD_LABELS, f"{skill}.{option} has no label"
    assert set(AGG_LABELS) == {"sum", "count", "avg", "min", "max"}


def test_series_request_flags_additive_aggregates():
    assert SeriesRequest(**_series(agg="sum")).is_additive
    assert SeriesRequest(**_series(agg="count")).is_additive
    assert not SeriesRequest(**_series(agg="avg")).is_additive


def test_envelope_artifact_view_drops_rows_and_keeps_flag():
    env = ResultEnvelope(
        skill="forecast",
        params={"horizon": 8},
        method_used="AutoETS",
        columns=["ts", "actual"],
        rows=[{"ts": "2026-01-01", "actual": 1.0}],
        chart_spec=ChartSpec(series=[ChartSeriesSpec(role="actual", label="Actual", column="actual")]),
        validation=Validation(metric="WAPE", value=0.064, band="good", basis="CV"),
        low_confidence=True,
        egress=Egress(tier="A", rows_sent_to_model=1, columns=["ts", "value"]),
        engine=EngineInfo(name="statsforecast", version="2.0"),
        provenance=Provenance(query_ts="2026-09-14T00:00:00Z"),
        details=ModelDetails(method_used="AutoETS"),
    )
    view = env.artifact_view()
    assert "rows" not in view and "columns" not in view
    assert view["low_confidence"] is True
    assert view["schema_version"] == CONTRACT_VERSION
    assert view["chart_spec"]["series"][0]["role"] == "actual"
