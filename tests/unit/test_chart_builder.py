"""Unit tests for the server-side chart builder, profiler, and result cache."""

from __future__ import annotations

from src.api.chart_builder import build_chart_option, profile_dataset, summarize_profile
from src.api.result_cache import ResultCache


def _dataset(rows, columns):
    return {"columns": columns, "rows": rows}


def _bar_spec(**over):
    spec = {
        "chart_type": "bar",
        "x": "region",
        "y": ["revenue"],
        "series": None,
        "aggregate": "sum",
        "sort": "none",
        "top_n": None,
        "title": "Revenue by region",
        "x_label": None,
        "y_label": None,
        "value_format": "number",
        "stacked": False,
        "smooth": False,
    }
    spec.update(over)
    return spec


# ── profiling ────────────────────────────────────────────────────────────────

def test_profile_detects_types_and_counts():
    rows = [{"region": "West", "revenue": 10}, {"region": "East", "revenue": 20}]
    prof = profile_dataset(_dataset(rows, ["region", "revenue"]))
    assert prof["row_count"] == 2
    by_name = {c["name"]: c for c in prof["columns"]}
    assert by_name["revenue"]["type"] == "numeric"
    assert by_name["revenue"]["sum"] == 30
    assert by_name["region"]["type"] == "category"
    assert by_name["region"]["distinct"] == 2


def test_profile_scan_cap_limits_stat_scan_but_not_count():
    rows = [{"v": 1} for _ in range(10)]
    prof = profile_dataset(_dataset(rows, ["v"]), scan_cap=3)
    assert prof["row_count"] == 10   # true count is exact
    assert prof["scanned"] == 3      # but stats only scanned 3 rows


def test_summarize_profile_renders_full_data_stats():
    rows = [{"region": "West", "revenue": 10}, {"region": "East", "revenue": 30}]
    text = summarize_profile(profile_dataset(_dataset(rows, ["region", "revenue"])))
    assert "Rows: 2" in text
    assert "revenue (numeric)" in text and "sum=40" in text
    assert "region (category)" in text and "distinct=2" in text


# ── aggregation uses the FULL dataset ──────────────────────────────────────────

def test_bar_aggregates_every_row():
    # 1000 rows alternating A/B, revenue 1 each → A=500, B=500. Proves the build
    # uses all rows, not a 10-row sample.
    rows = [{"region": "A" if i % 2 == 0 else "B", "revenue": 1} for i in range(1000)]
    opt = build_chart_option(_bar_spec(), _dataset(rows, ["region", "revenue"]))
    cats = opt["xAxis"]["data"]
    data = opt["series"][0]["data"]
    totals = dict(zip(cats, data))
    assert totals == {"A": 500, "B": 500}


def test_bar_sort_desc_orders_categories():
    rows = [
        {"region": "West", "revenue": 10},
        {"region": "East", "revenue": 20},
        {"region": "West", "revenue": 30},
        {"region": "East", "revenue": 5},
    ]
    opt = build_chart_option(_bar_spec(sort="desc"), _dataset(rows, ["region", "revenue"]))
    assert opt["xAxis"]["data"] == ["West", "East"]   # 40 then 25
    assert opt["series"][0]["data"] == [40, 25]


def test_top_n_collapses_tail_into_other():
    rows = [{"region": f"R{i}", "revenue": 100 - i} for i in range(10)]
    opt = build_chart_option(
        _bar_spec(sort="desc", top_n=3), _dataset(rows, ["region", "revenue"])
    )
    cats = opt["xAxis"]["data"]
    assert cats[:3] == ["R0", "R1", "R2"]
    assert cats[-1] == "Other"
    # Other == sum of the remaining 7 (revenues 90..91? compute): revenue=100-i.
    expected_other = sum(100 - i for i in range(3, 10))
    assert opt["series"][0]["data"][-1] == expected_other


def test_grouped_series_produce_multiple_series():
    rows = [
        {"region": "West", "product": "A", "revenue": 10},
        {"region": "West", "product": "B", "revenue": 5},
        {"region": "East", "product": "A", "revenue": 7},
    ]
    spec = _bar_spec(series="product")
    opt = build_chart_option(spec, _dataset(rows, ["region", "product", "revenue"]))
    names = sorted(s["name"] for s in opt["series"])
    assert names == ["A", "B"]


def test_pie_uses_aggregated_values():
    rows = [
        {"region": "West", "revenue": 10},
        {"region": "East", "revenue": 20},
        {"region": "West", "revenue": 30},
    ]
    spec = _bar_spec(chart_type="pie")
    opt = build_chart_option(spec, _dataset(rows, ["region", "revenue"]))
    data = {d["name"]: d["value"] for d in opt["series"][0]["data"]}
    assert data == {"West": 40, "East": 20}


def test_positional_rows_supported():
    rows = [["West", 10], ["East", 20], ["West", 30]]
    opt = build_chart_option(_bar_spec(), _dataset(rows, ["region", "revenue"]))
    totals = dict(zip(opt["xAxis"]["data"], opt["series"][0]["data"]))
    assert totals == {"West": 40, "East": 20}


# ── dates & time ───────────────────────────────────────────────────────────────

def test_line_orders_dates_chronologically():
    rows = [
        {"d": "2024-03-01", "revenue": 3},
        {"d": "2024-01-01", "revenue": 1},
        {"d": "2024-02-01", "revenue": 2},
    ]
    opt = build_chart_option(
        _bar_spec(chart_type="line", x="d", sort="none"), _dataset(rows, ["d", "revenue"])
    )
    assert opt["xAxis"]["data"] == ["2024-01-01", "2024-02-01", "2024-03-01"]
    assert opt["series"][0]["data"] == [1, 2, 3]
    assert opt["series"][0]["type"] == "line"


def test_composite_x_parts_join_and_chronological_order():
    rows = [
        {"year": 2024, "month": 2, "revenue": 20},
        {"year": 2023, "month": 12, "revenue": 12},
        {"year": 2024, "month": 1, "revenue": 10},
    ]
    opt = build_chart_option(
        _bar_spec(chart_type="line", x="year", x_parts=["year", "month"]),
        _dataset(rows, ["year", "month", "revenue"]),
    )
    # Joined + zero-padded month, sorted in time order.
    assert opt["xAxis"]["data"] == ["2023-12", "2024-01", "2024-02"]
    assert opt["series"][0]["data"] == [12, 10, 20]


def test_composite_x_parts_skips_top_n_truncation():
    # 24 months of data; top_n must NOT collapse a time axis into "Other".
    rows = [
        {"year": 2023 + (m // 12), "month": (m % 12) + 1, "revenue": m}
        for m in range(24)
    ]
    opt = build_chart_option(
        _bar_spec(chart_type="line", x_parts=["year", "month"], sort="desc", top_n=5),
        _dataset(rows, ["year", "month", "revenue"]),
    )
    assert "Other" not in opt["xAxis"]["data"]
    assert len(opt["xAxis"]["data"]) == 24


# ── value-format hint ──────────────────────────────────────────────────────────

def test_jeen_format_hint_emitted():
    ds = _dataset([{"region": "A", "revenue": 1_000_000}], ["region", "revenue"])
    cur = build_chart_option(_bar_spec(value_format="currency"), ds)
    # Currency WITHOUT a known symbol → empty symbol (never assume "$").
    assert cur["jeenFormat"] == {"kind": "currency", "compact": True, "symbol": ""}
    pct = build_chart_option(_bar_spec(value_format="percent"), ds)
    assert pct["jeenFormat"] == {"kind": "percent", "compact": False, "symbol": ""}
    num = build_chart_option(_bar_spec(value_format="number"), ds)
    assert num["jeenFormat"]["symbol"] == ""


def test_currency_symbol_passed_through_only_for_currency():
    ds = _dataset([{"region": "A", "revenue": 5}], ["region", "revenue"])
    eur = build_chart_option(_bar_spec(value_format="currency", currency_symbol="€"), ds)
    assert eur["jeenFormat"] == {"kind": "currency", "compact": True, "symbol": "€"}
    # A symbol on a non-currency format must be ignored.
    num = build_chart_option(_bar_spec(value_format="number", currency_symbol="€"), ds)
    assert num["jeenFormat"]["symbol"] == ""


# ── aggregates ─────────────────────────────────────────────────────────────────

def test_aggregates_avg_count_min_max():
    rows = [{"g": "A", "v": 10}, {"g": "A", "v": 20}, {"g": "B", "v": 5}]
    cols = ["g", "v"]

    def totals(agg):
        opt = build_chart_option(_bar_spec(x="g", y=["v"], aggregate=agg), _dataset(rows, cols))
        return dict(zip(opt["xAxis"]["data"], opt["series"][0]["data"]))

    assert totals("avg") == {"A": 15, "B": 5}
    assert totals("count") == {"A": 2, "B": 1}
    assert totals("min")["A"] == 10
    assert totals("max")["A"] == 20


def test_null_measures_are_skipped():
    rows = [{"g": "A", "v": None}, {"g": "A", "v": 10}, {"g": "B", "v": None}]
    opt = build_chart_option(_bar_spec(x="g", y=["v"], aggregate="sum"), _dataset(rows, ["g", "v"]))
    d = dict(zip(opt["xAxis"]["data"], opt["series"][0]["data"]))
    assert d["A"] == 10
    assert d["B"] is None  # only nulls → no value


# ── every chart type builds a valid option ─────────────────────────────────────

def test_all_chart_types_build_valid_options():
    rows = [
        {"region": "West", "product": "A", "revenue": 10, "cost": 4},
        {"region": "East", "product": "B", "revenue": 20, "cost": 9},
        {"region": "West", "product": "B", "revenue": 30, "cost": 7},
    ]
    ds = _dataset(rows, ["region", "product", "revenue", "cost"])

    for ct in ["bar", "horizontal_bar", "line", "area", "stacked_bar",
               "stacked_area", "pie", "donut", "gauge"]:
        opt = build_chart_option(_bar_spec(chart_type=ct), ds)
        assert opt.get("series"), f"{ct} produced no series"

    scatter = build_chart_option(_bar_spec(chart_type="scatter", x="cost", y=["revenue"]), ds)
    assert scatter["series"][0]["type"] == "scatter"

    combo = build_chart_option(_bar_spec(chart_type="combo", x="region", y=["revenue", "cost"]), ds)
    assert len(combo["series"]) >= 1

    heatmap = build_chart_option(
        _bar_spec(chart_type="heatmap", x="region", series="product", y=["revenue"]), ds
    )
    assert heatmap["series"][0]["type"] == "heatmap"


def test_grouped_series_align_to_categories():
    rows = [
        {"region": "West", "product": "A", "revenue": 10},
        {"region": "East", "product": "A", "revenue": 7},
        {"region": "West", "product": "B", "revenue": 5},
    ]
    opt = build_chart_option(
        _bar_spec(series="product"), _dataset(rows, ["region", "product", "revenue"])
    )
    cats = opt["xAxis"]["data"]
    for s in opt["series"]:
        assert len(s["data"]) == len(cats)


# ── ECharts rendering best practices ───────────────────────────────────────────

def test_line_series_use_lttb_sampling():
    rows = [{"region": f"d{i}", "revenue": i} for i in range(5)]
    opt = build_chart_option(_bar_spec(chart_type="line"), _dataset(rows, ["region", "revenue"]))
    assert opt["series"][0]["sampling"] == "lttb"


def test_scatter_enables_large_mode():
    rows = [{"x": i, "y": i * 2} for i in range(5)]
    opt = build_chart_option(
        _bar_spec(chart_type="scatter", x="x", y=["y"]), _dataset(rows, ["x", "y"])
    )
    s = opt["series"][0]
    assert s["large"] is True and s["largeThreshold"] == 2000


def test_data_zoom_added_only_for_many_categories():
    few = [{"region": f"r{i}", "revenue": i} for i in range(10)]
    opt_few = build_chart_option(_bar_spec(), _dataset(few, ["region", "revenue"]))
    assert "dataZoom" not in opt_few

    many = [{"region": f"r{i}", "revenue": i} for i in range(60)]
    opt_many = build_chart_option(_bar_spec(), _dataset(many, ["region", "revenue"]))
    assert opt_many.get("dataZoom"), "expected zoom controls for many categories"
    assert any(z["type"] == "slider" for z in opt_many["dataZoom"])
    # the slider sits on the x axis for vertical bars
    assert opt_many["dataZoom"][0].get("xAxisIndex") == 0


def test_horizontal_bar_zoom_targets_y_axis():
    many = [{"region": f"r{i}", "revenue": i} for i in range(60)]
    opt = build_chart_option(
        _bar_spec(chart_type="horizontal_bar"), _dataset(many, ["region", "revenue"])
    )
    assert opt["dataZoom"][0].get("yAxisIndex") == 0


def test_multi_series_tooltip_ordered_by_value():
    rows = [
        {"region": "West", "product": "A", "revenue": 10},
        {"region": "East", "product": "B", "revenue": 20},
    ]
    opt = build_chart_option(
        _bar_spec(series="product"), _dataset(rows, ["region", "product", "revenue"])
    )
    assert opt["tooltip"]["order"] == "valueDesc"


def test_pie_avoids_label_overlap():
    rows = [{"region": f"r{i}", "revenue": i + 1} for i in range(8)]
    opt = build_chart_option(_bar_spec(chart_type="pie"), _dataset(rows, ["region", "revenue"]))
    series = opt["series"][0]
    assert series["minAngle"] >= 1
    assert series["labelLayout"] == {"hideOverlap": True}


# ── result cache ───────────────────────────────────────────────────────────────

def test_cache_put_get_roundtrip():
    cache = ResultCache(max_entries=4, ttl_seconds=60)
    ds = _dataset([{"a": 1}], ["a"])
    cache.put(user_id="u1", connection="c1", query_id="q1", dataset=ds)
    got = cache.get(user_id="u1", connection="c1", query_id="q1")
    assert got is not None
    assert got["rows"] == [{"a": 1}]


def test_cache_isolation_by_key():
    cache = ResultCache()
    cache.put(user_id="u1", connection="c1", query_id="q1", dataset=_dataset([{"a": 1}], ["a"]))
    # Different user / connection / query_id → miss.
    assert cache.get(user_id="u2", connection="c1", query_id="q1") is None
    assert cache.get(user_id="u1", connection="c2", query_id="q1") is None
    assert cache.get(user_id="u1", connection="c1", query_id="q2") is None


def test_cache_lru_eviction():
    cache = ResultCache(max_entries=2, ttl_seconds=60)
    for i in range(3):
        cache.put(user_id="u", connection="c", query_id=f"q{i}", dataset=_dataset([{"a": i}], ["a"]))
    # q0 evicted (oldest), q1/q2 retained.
    assert cache.get(user_id="u", connection="c", query_id="q0") is None
    assert cache.get(user_id="u", connection="c", query_id="q1") is not None
    assert cache.get(user_id="u", connection="c", query_id="q2") is not None


def test_cache_ignores_empty_dataset():
    cache = ResultCache()
    cache.put(user_id="u", connection="c", query_id="q", dataset={"columns": ["a"], "rows": []})
    assert cache.get(user_id="u", connection="c", query_id="q") is None


def test_cache_per_user_cap_keeps_only_recent():
    cache = ResultCache(max_entries=100, ttl_seconds=60, per_user_max=3)
    for i in range(5):
        cache.put(user_id="u", connection="c", query_id=f"q{i}", dataset=_dataset([{"a": i}], ["a"]))
    # Only the 3 most-recent queries survive for this user.
    assert cache.get(user_id="u", connection="c", query_id="q0") is None
    assert cache.get(user_id="u", connection="c", query_id="q1") is None
    assert cache.get(user_id="u", connection="c", query_id="q2") is not None
    assert cache.get(user_id="u", connection="c", query_id="q4") is not None


def test_cache_per_user_cap_isolated_between_users():
    cache = ResultCache(max_entries=100, ttl_seconds=60, per_user_max=2)
    # A busy user runs many queries...
    for i in range(5):
        cache.put(user_id="busy", connection="c", query_id=f"q{i}", dataset=_dataset([{"a": i}], ["a"]))
    # ...which must NOT evict another user's entry (no noisy-neighbour).
    cache.put(user_id="quiet", connection="c", query_id="q", dataset=_dataset([{"a": 1}], ["a"]))
    assert cache.get(user_id="quiet", connection="c", query_id="q") is not None
    assert cache.get(user_id="busy", connection="c", query_id="q4") is not None
    assert cache.get(user_id="busy", connection="c", query_id="q2") is None
