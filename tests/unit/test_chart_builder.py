"""Unit tests for the server-side chart builder, profiler, and result cache."""

from __future__ import annotations

import json
from pathlib import Path

from src.api.chart_builder import build_chart_option, profile_dataset, summarize_profile
from src.api.map_locations import (
    _COUNTRY_ALIASES,
    _ISRAEL_DISTRICT_ALIASES,
    map_feature_names,
)
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

def test_line_with_real_dates_uses_time_axis():
    rows = [
        {"d": "2024-03-01", "revenue": 3},
        {"d": "2024-01-01", "revenue": 1},
        {"d": "2024-02-01", "revenue": 2},
    ]
    opt = build_chart_option(
        _bar_spec(chart_type="line", x="d", sort="none"), _dataset(rows, ["d", "revenue"])
    )
    # A genuine date column gets a real time axis with chronological [x, y] pairs.
    assert opt["xAxis"]["type"] == "time"
    assert opt["series"][0]["data"] == [
        ["2024-01-01", 1], ["2024-02-01", 2], ["2024-03-01", 3]
    ]
    assert opt["series"][0]["type"] == "line"


def test_categorical_line_keeps_category_axis():
    # Non-date x stays on a category axis with a plain value list.
    rows = [{"region": "West", "revenue": 3}, {"region": "East", "revenue": 1}]
    opt = build_chart_option(
        _bar_spec(chart_type="line", x="region", sort="none"),
        _dataset(rows, ["region", "revenue"]),
    )
    assert opt["xAxis"]["type"] == "category"
    assert opt["series"][0]["data"] == [3, 1]


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


def test_percent_fractions_get_x100_scale():
    # Stored as 0–1 fractions → scaled ×100 so labels read 34%, not 0.34%.
    rows = [{"region": "A", "rate": 0.34}, {"region": "B", "rate": 0.12}]
    opt = build_chart_option(
        _bar_spec(value_format="percent", x="region", y=["rate"]),
        _dataset(rows, ["region", "rate"]),
    )
    assert opt["jeenFormat"]["kind"] == "percent"
    assert opt["jeenFormat"]["scale"] == 100


def test_percent_already_0_to_100_has_no_scale():
    rows = [{"region": "A", "rate": 34}, {"region": "B", "rate": 12}]
    opt = build_chart_option(
        _bar_spec(value_format="percent", x="region", y=["rate"]),
        _dataset(rows, ["region", "rate"]),
    )
    assert opt["jeenFormat"]["kind"] == "percent"
    assert "scale" not in opt["jeenFormat"]


# ── negative values ────────────────────────────────────────────────────────────

def test_negative_bars_colored_with_zero_baseline():
    rows = [{"region": "A", "profit": 10}, {"region": "B", "profit": -4}]
    opt = build_chart_option(
        _bar_spec(x="region", y=["profit"], sort="none"),
        _dataset(rows, ["region", "profit"]),
    )
    data = opt["series"][0]["data"]
    assert data[0] == 10                       # positive stays a plain number
    assert data[1]["value"] == -4              # negative wrapped with a colour
    assert data[1]["itemStyle"]["color"]
    assert "markLine" in opt["series"][0]      # explicit zero baseline


def test_all_positive_bars_have_no_negative_styling():
    rows = [{"region": "A", "v": 10}, {"region": "B", "v": 4}]
    opt = build_chart_option(_bar_spec(x="region", y=["v"]), _dataset(rows, ["region", "v"]))
    assert opt["series"][0]["data"] == [10, 4]
    assert "markLine" not in opt["series"][0]


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


def test_country_choropleth_map_builds_with_aliases():
    rows = [
        {"country": "USA", "customer_count": 6828},
        {"country": "United Kingdom", "customer_count": 1944},
        {"country": "Canada", "customer_count": 1553},
    ]
    opt = build_chart_option(
        _bar_spec(
            chart_type="map",
            map_mode="choropleth",
            map_name="world",
            x="country",
            location="country",
            y=["customer_count"],
            value="customer_count",
            title="Customers by Country",
        ),
        _dataset(rows, ["country", "customer_count"]),
    )
    assert opt["series"][0]["type"] == "map"
    assert opt["series"][0]["map"] == "world"
    data = {item["name"]: item["value"] for item in opt["series"][0]["data"]}
    assert data["United States"] == 6828
    assert data["United Kingdom"] == 1944
    assert opt["jeenMap"]["matched"] == 3


def test_country_choropleth_reports_unknown_locations():
    rows = [
        {"country": "USA", "customer_count": 10},
        {"country": "Atlantis", "customer_count": 5},
    ]
    opt = build_chart_option(
        _bar_spec(
            chart_type="map",
            map_mode="choropleth",
            map_name="world",
            x="country",
            location="country",
            y=["customer_count"],
            value="customer_count",
        ),
        _dataset(rows, ["country", "customer_count"]),
    )
    data = {item["name"]: item["value"] for item in opt["series"][0]["data"]}
    assert data == {"United States": 10}
    assert opt["jeenMap"]["matched"] == 1
    assert opt["jeenMap"]["unmatched"] == ["Atlantis"]


def test_israel_district_choropleth_matches_hebrew_alias():
    rows = [
        {"district": "מחוז תל אביב", "customers": 120},
        {"district": "Central", "customers": 80},
    ]
    opt = build_chart_option(
        _bar_spec(
            chart_type="map",
            map_mode="choropleth",
            map_name="israel_districts",
            x="district",
            location="district",
            y=["customers"],
            value="customers",
        ),
        _dataset(rows, ["district", "customers"]),
    )
    data = {item["name"]: item["value"] for item in opt["series"][0]["data"]}
    assert data["Tel Aviv District"] == 120
    assert data["Center District"] == 80


def test_israel_city_points_use_local_lookup_without_lat_lng():
    rows = [
        {"city": "תל אביב", "sales": 100},
        {"city": "Haifa", "sales": 50},
    ]
    opt = build_chart_option(
        _bar_spec(
            chart_type="map",
            map_mode="points",
            map_name="israel_districts",
            x="city",
            location="city",
            y=["sales"],
            value="sales",
        ),
        _dataset(rows, ["city", "sales"]),
    )
    assert opt["geo"]["map"] == "israel_districts"
    assert opt["series"][0]["type"] == "scatter"
    assert len(opt["series"][0]["data"]) == 2
    point = opt["series"][0]["data"][0]
    assert point["name"] == "Tel Aviv"
    assert len(point["value"]) == 3


def test_osm_map_emits_one_point_overlay_and_aggregates_coordinates():
    rows = [
        {"store": "North", "latitude": 32.08, "longitude": 34.78, "sales": 10, "orders": 2},
        {"store": "North duplicate", "latitude": 32.08, "longitude": 34.78, "sales": 20, "orders": 4},
        {"store": "South", "latitude": 31.77, "longitude": 35.21, "sales": 5, "orders": 1},
    ]
    opt = build_chart_option(
        _bar_spec(
            chart_type="osm_map",
            x="store",
            location="store",
            latitude="latitude",
            longitude="longitude",
            y=["sales", "orders"],
            value="sales",
            value2="orders",
            aggregate="sum",
        ),
        _dataset(rows, ["store", "latitude", "longitude", "sales", "orders"]),
    )
    assert opt["series"] == []
    osm = opt["jeenOsmMap"]
    assert osm["basemap"]["tileUrl"] == "/api/map-tiles/{z}/{x}/{y}"
    assert osm["layers"]["basemaps"][0]["id"] == "standard"
    assert osm["layers"]["basemaps"][0]["tileUrl"] == "/api/map-tiles/{z}/{x}/{y}"
    overlay = osm["overlays"][0]
    assert overlay["type"] == "circles"
    assert overlay["metric"] == "sales"
    assert overlay["sizeMetric"] == "orders"
    assert len(overlay["points"]) == 2
    north = next(point for point in overlay["points"] if point["lat"] == 32.08)
    assert north["value"] == 30
    assert north["value2"] == 6
    assert north["rowCount"] == 2
    assert north["rowIndexes"] == [0, 1]
    assert north["placeKey"] == "north"
    assert osm["matched"] == 2
    assert osm["rowCount"] == 3


def test_osm_map_counts_raw_geography_rows_without_using_key_as_measure():
    rows = [
        {"GeographyKey": 1, "City": "Paris", "State": "Texas", "Country": "United States"},
        {"GeographyKey": 2, "City": "Paris", "State": "Texas", "Country": "United States"},
    ]
    opt = build_chart_option(
        _bar_spec(
            chart_type="osm_map",
            x="City",
            location="City",
            location_parts={"place": "City", "admin1": "State", "country": "Country"},
            y=["__row_count__"],
            value="__row_count__",
            aggregate="count",
            resolved_locations={
                "paris|texas|united states": {
                    "status": "resolved", "lat": 33.66, "lng": -95.55,
                },
            },
        ),
        _dataset(rows, ["GeographyKey", "City", "State", "Country"]),
    )

    point = opt["jeenOsmMap"]["overlays"][0]["points"][0]
    assert point["label"] == "Paris, Texas, United States"
    assert point["value"] == 2
    assert point["rowIndexes"] == [0, 1]


def test_osm_map_uses_persisted_place_resolution_without_network():
    rows = [{"place": "Elsewhere", "sales": 8}]
    opt = build_chart_option(
        _bar_spec(
            chart_type="osm_map",
            x="place",
            location="place",
            y=["sales"],
            value="sales",
            resolved_locations={
                "elsewhere": {"status": "resolved", "lat": 48.8566, "lng": 2.3522},
            },
        ),
        _dataset(rows, ["place", "sales"]),
    )
    point = opt["jeenOsmMap"]["overlays"][0]["points"][0]
    assert point["lat"] == 48.8566
    assert point["lng"] == 2.3522
    assert opt["jeenOsmMap"]["unmatchedCount"] == 0


def test_osm_map_rejects_invalid_coordinates_as_unmatched():
    rows = [{"place": "Impossible", "latitude": 190, "longitude": 400, "sales": 8}]
    opt = build_chart_option(
        _bar_spec(
            chart_type="osm_map",
            x="place",
            location="place",
            latitude="latitude",
            longitude="longitude",
            y=["sales"],
            value="sales",
        ),
        _dataset(rows, ["place", "latitude", "longitude", "sales"]),
    )
    assert opt["jeenOsmMap"]["overlays"][0]["points"] == []
    assert opt["jeenOsmMap"]["unmatched"] == ["Impossible"]


def test_osm_map_reports_unmatched_status_by_unique_location():
    rows = [
        {"city": "Unresolved City", "sales": 3},
        {"city": "Unresolved City", "sales": 4},
    ]
    opt = build_chart_option(
        _bar_spec(
            chart_type="osm_map",
            x="city",
            location="city",
            y=["sales"],
            value="sales",
            resolved_locations={
                "unresolved city": {"status": "limited", "source": "limit"},
            },
        ),
        _dataset(rows, ["city", "sales"]),
    )

    assert opt["jeenOsmMap"]["unmatchedCount"] == 1
    assert opt["jeenOsmMap"]["unmatchedByStatus"] == {"limited": 1}




def test_map_assets_have_no_dateline_rendering_jumps():
    asset_root = Path(__file__).resolve().parents[2] / "src" / "static" / "chart-feature" / "assets" / "maps"
    for filename in ["world.json", "world_detailed.json", "israel_districts.json"]:
        _assert_no_dateline_jumps(asset_root / filename)


def _assert_no_dateline_jumps(asset_path):
    geojson = json.loads(asset_path.read_text(encoding="utf-8"))
    names = {feature["properties"]["name"] for feature in geojson["features"]}
    if asset_path.name.startswith("world"):
        assert "Antarctica" not in names

    jumps = []
    for feature in geojson["features"]:
        geometry = feature["geometry"]
        polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        for polygon in polygons:
            for ring in polygon:
                jumps.extend(
                    (feature["properties"]["name"], prev, cur)
                    for prev, cur in zip(ring, ring[1:])
                    if abs(cur[0] - prev[0]) > 180
                )
    assert jumps == []


def test_alias_targets_exist_in_map_assets():
    world_names = map_feature_names("world")
    detailed_names = map_feature_names("world_detailed")
    israel_names = map_feature_names("israel_districts")

    country_targets = set(_COUNTRY_ALIASES.values())
    assert country_targets <= world_names
    assert country_targets <= detailed_names
    assert set(_ISRAEL_DISTRICT_ALIASES.values()) <= israel_names


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


# ── combo (grouped bars + secondary-axis line) ───────────────────────────────

def _combo_dataset():
    rows = [
        {"month": "Jan", "revenue_2006": 100, "revenue_2007": 150, "yoy_change_pct": 50},
        {"month": "Feb", "revenue_2006": 200, "revenue_2007": 180, "yoy_change_pct": -10},
    ]
    cols = ["month", "revenue_2006", "revenue_2007", "yoy_change_pct"]
    return _dataset(rows, cols)


def test_combo_groups_same_scale_bars_and_puts_change_on_secondary_line():
    spec = _bar_spec(
        chart_type="combo", x="month",
        y=["revenue_2006", "revenue_2007", "yoy_change_pct"],
    )
    opt = build_chart_option(spec, _combo_dataset())
    by_name = {s["name"]: s for s in opt["series"]}
    # Two revenue columns = grouped bars on the primary (left) axis.
    assert by_name["revenue_2006"]["type"] == "bar"
    assert by_name["revenue_2006"]["yAxisIndex"] == 0
    assert by_name["revenue_2007"]["type"] == "bar"
    assert by_name["revenue_2007"]["yAxisIndex"] == 0
    # The %-change column = line on the secondary (right) axis.
    assert by_name["yoy_change_pct"]["type"] == "line"
    assert by_name["yoy_change_pct"]["yAxisIndex"] == 1
    # Two value axes present.
    assert isinstance(opt["yAxis"], list) and len(opt["yAxis"]) == 2


def test_combo_respects_explicit_secondary_y():
    spec = _bar_spec(
        chart_type="combo", x="month",
        y=["revenue_2006", "revenue_2007"],
        secondary_y=["revenue_2007"],
    )
    opt = build_chart_option(spec, _combo_dataset())
    by_name = {s["name"]: s for s in opt["series"]}
    assert by_name["revenue_2006"]["type"] == "bar"
    assert by_name["revenue_2007"]["type"] == "line"
    assert by_name["revenue_2007"]["yAxisIndex"] == 1


def test_combo_fallback_first_is_bar_when_no_secondary_detected():
    # No percent/change measure and no explicit secondary_y → first is a bar,
    # the rest become secondary-axis lines (legacy behaviour preserved).
    spec = _bar_spec(chart_type="combo", x="region", y=["revenue", "cost"])
    rows = [{"region": "W", "revenue": 10, "cost": 4}, {"region": "E", "revenue": 8, "cost": 3}]
    opt = build_chart_option(spec, _dataset(rows, ["region", "revenue", "cost"]))
    by_name = {s["name"]: s for s in opt["series"]}
    assert by_name["revenue"]["type"] == "bar" and by_name["revenue"]["yAxisIndex"] == 0
    assert by_name["cost"]["type"] == "line" and by_name["cost"]["yAxisIndex"] == 1


def test_combo_emits_per_axis_and_per_series_formats():
    # Bars (revenue) format as currency on the left; the %-change line formats as
    # percent on the right — each axis and series carries its own format hint.
    spec = _bar_spec(
        chart_type="combo", x="month",
        y=["revenue_2006", "revenue_2007", "yoy_change_pct"],
        value_format="currency", currency_symbol="$",
    )
    opt = build_chart_option(spec, _combo_dataset())
    assert opt["yAxis"][0]["jeenFormat"]["kind"] == "currency"
    assert opt["yAxis"][0]["jeenFormat"]["symbol"] == "$"
    assert opt["yAxis"][1]["jeenFormat"]["kind"] == "percent"
    by_name = {s["name"]: s for s in opt["series"]}
    assert by_name["revenue_2006"]["jeenFormat"]["kind"] == "currency"
    assert by_name["yoy_change_pct"]["jeenFormat"]["kind"] == "percent"


def test_combo_secondary_line_gets_zero_baseline_when_negative():
    spec = _bar_spec(
        chart_type="combo", x="month",
        y=["revenue_2006", "revenue_2007", "yoy_change_pct"],
    )
    opt = build_chart_option(spec, _combo_dataset())  # yoy has -10
    line = next(s for s in opt["series"] if s["name"] == "yoy_change_pct")
    assert "markLine" in line


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
