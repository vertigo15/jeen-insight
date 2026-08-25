"""Server-side chart construction.

The LLM only *decides* how to visualize (chart type, column roles, aggregation,
sort, top-N, formatting) — see ``routes/charts.py``. This module *builds* the
actual ECharts option from the FULL result set, so the chart always reflects
every row and numbers are exact (the model never transcribes values).

Two pure functions, no FastAPI/DB deps so they're trivially unit-testable:
  * ``profile_dataset``     — compact stats the LLM reasons over.
  * ``build_chart_option``  — spec + full rows -> ECharts option (pure JSON).

Rows may be dicts (column->value, as ``run_sql`` returns) or positional lists.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from src.api.map_locations import (
    canonical_region,
    infer_map_name,
    lookup_israel_city,
)
from src.api.map_geocoding import location_key, valid_coordinates
from src.api.map_layers import browser_map_layers

# Measure names that belong on a combo chart's SECONDARY (right) axis as a line:
# percentages, rates and period-over-period change metrics live on a very
# different scale than the primary measures they're compared against.
_SECONDARY_MEASURE_RE = re.compile(
    r"(pct|percent|ratio|rate|change|growth|delta|diff|margin|yoy|mom|qoq|index)",
    re.I,
)

# Narrower than the above: names that read as a PERCENTAGE (so we format with %).
_PERCENT_MEASURE_RE = re.compile(
    r"(pct|percent|rate|ratio|margin|share|growth|yoy|mom|qoq)", re.I
)

# Negative bars/points get a distinct (red) colour so losses read at a glance.
_NEG_COLOR = "#d9534f"
_MAP_PALETTES: Dict[str, List[str]] = {
    "blue": ["#f7fbff", "#c6dbef", "#6baed6", "#2171b5", "#08306b"],
    "green": ["#f7fcf5", "#c7e9c0", "#74c476", "#238b45", "#00441b"],
    "purple": ["#fcfbfd", "#dadaeb", "#9e9ac8", "#6a51a3", "#3f007d"],
    "orange": ["#fff7ec", "#fdd49e", "#fc8d59", "#d94801", "#7f2704"],
}
_MAP_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "world": {
        "layoutCenter": ["50%", "50%"],
        "layoutSize": "96%",
        "aspectScale": 0.86,
        "zoom": 1.02,
        "scaleLimit": {"min": 1, "max": 8},
        "showLabels": False,
        "noDataColor": "#eef2f7",
        "borderColor": "#ffffff",
        "borderWidth": 0.65,
    },
    "world_detailed": {
        "layoutCenter": ["50%", "50%"],
        "layoutSize": "98%",
        "aspectScale": 0.86,
        "zoom": 1.02,
        "scaleLimit": {"min": 1, "max": 10},
        "showLabels": False,
        "noDataColor": "#eef2f7",
        "borderColor": "#ffffff",
        "borderWidth": 0.5,
    },
    "israel_districts": {
        "layoutCenter": ["50%", "51%"],
        "layoutSize": "92%",
        "aspectScale": 0.78,
        "zoom": 1.15,
        "scaleLimit": {"min": 1, "max": 12},
        "showLabels": True,
        "noDataColor": "#eef2f7",
        "borderColor": "#ffffff",
        "borderWidth": 1.1,
    },
}


def _map_defaults(map_name: str) -> Dict[str, Any]:
    return _MAP_DEFAULTS.get(map_name) or _MAP_DEFAULTS["world"]


def _map_palette(spec: Dict[str, Any]) -> tuple[str, List[str]]:
    name = str(spec.get("map_palette") or "blue").strip().lower()
    if name not in _MAP_PALETTES:
        name = "blue"
    return name, _MAP_PALETTES[name]


def _map_view_options(map_name: str) -> Dict[str, Any]:
    defaults = _map_defaults(map_name)
    return {
        "layoutCenter": defaults["layoutCenter"],
        "layoutSize": defaults["layoutSize"],
        "aspectScale": defaults["aspectScale"],
        "zoom": defaults["zoom"],
        "scaleLimit": defaults["scaleLimit"],
    }


def _fmt_meta(value_format: str, symbol: str = "", scale: float = 1) -> Dict[str, Any]:
    """Client value-format hint. ``scale`` (only emitted when ≠ 1) multiplies the
    value before formatting — used to render 0–1 fractions as 0–100 percent."""
    meta: Dict[str, Any] = {
        "kind": value_format,
        "compact": value_format != "percent",
        "symbol": symbol if value_format == "currency" else "",
    }
    if scale and scale != 1:
        meta["scale"] = scale
    return meta


def _percent_scale(values) -> float:
    """Decide whether a percent measure is stored as a fraction (0.34) or already
    as a percentage (34). Fractions (|max| ≤ 1.5) are scaled ×100 for display."""
    mx = 0.0
    for v in values:
        if isinstance(v, (int, float)) and math.isfinite(v):
            mx = max(mx, abs(v))
    return 100.0 if 0 < mx <= 1.5 else 1.0


def _color_negatives(data: List[Any]) -> tuple[List[Any], bool]:
    """Wrap negative bar values so they render in ``_NEG_COLOR``; positives keep
    the series colour. Returns (new_data, had_negative)."""
    out: List[Any] = []
    had_neg = False
    for v in data:
        if isinstance(v, (int, float)) and math.isfinite(v) and v < 0:
            had_neg = True
            out.append({"value": v, "itemStyle": {"color": _NEG_COLOR}})
        else:
            out.append(v)
    return out, had_neg


def _zero_markline() -> Dict[str, Any]:
    """A subtle dashed line at zero so the baseline is obvious when a series
    spans positive and negative values."""
    return {
        "silent": True,
        "symbol": "none",
        "label": {"show": False},
        "lineStyle": {"color": "#9aa0a6", "type": "dashed", "width": 1},
        "data": [{"yAxis": 0}],
    }

OTHER_LABEL = "Other"
NULL_LABEL = "(null)"
_SINGLE = "\x00single"
_PROFILE_SCAN_CAP = 5000
_MAX_SAMPLE_CATEGORIES = 12
_PLOT_ROW_CAP = 5000  # categorical points beyond this are unreadable anyway

PALETTE = [
    "#5470c6", "#91cc75", "#fac858", "#ee6666", "#73c0de",
    "#3ba272", "#fc8452", "#9a60b4", "#ea7ccc", "#2f4554",
]


# ── cell helpers ────────────────────────────────────────────────────────────

def _to_number(cell: Any) -> Optional[float]:
    if cell is None or cell == "" or isinstance(cell, bool):
        return None
    if isinstance(cell, (int, float)):
        return float(cell) if math.isfinite(cell) else None
    s = str(cell).strip()
    for ch in "$€£¥, %":
        s = s.replace(ch, "")
    if s in ("", "-", "."):
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def _read_cell(row: Any, name: str, index: int) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    if isinstance(row, (list, tuple)):
        return row[index] if 0 <= index < len(row) else None
    return None


def _as_label(value: Any) -> str:
    if value is None or value == "":
        return NULL_LABEL
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _looks_like_date(value: Any) -> bool:
    if isinstance(value, (datetime, date)):
        return True
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s or len(s) < 6:
        return False
    # ISO-ish: 2024-01-02 or 2024/01/02 (optionally with time).
    head = s[:10]
    if len(head) == 10 and head[4] in "-/" and head[7] in "-/":
        return head[:4].isdigit()
    return False


def _round(n: float) -> float:
    if not isinstance(n, (int, float)) or not math.isfinite(n):
        return n
    return round(n, 4)


def _composite_label(row: Any, parts: List[tuple]) -> str:
    """Join several columns into one axis label, e.g. year=2024 + month=1 ->
    "2024-01". Sub-year integer parts (month/quarter/day) are zero-padded so a
    plain string sort is chronological."""
    bits: List[str] = []
    for i, (name, idx) in enumerate(parts):
        raw = _read_cell(row, name, idx)
        num = _to_number(raw)
        if num is not None and float(num).is_integer():
            iv = int(num)
            # First part is the year (kept as-is); later small ints are padded.
            bits.append(f"{iv:02d}" if i > 0 and 1 <= iv <= 31 else str(iv))
        else:
            bits.append(_as_label(raw))
    return "-".join(bits)


def _nice_ceil(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        return value or 1
    mag = 10 ** math.floor(math.log10(value))
    return math.ceil(value / mag) * mag


# ── profiling ────────────────────────────────────────────────────────────────

def _iter_rows(dataset: Dict[str, Any]) -> List[Any]:
    return dataset.get("rows") or dataset.get("data") or []


def profile_dataset(dataset: Dict[str, Any], scan_cap: int = _PROFILE_SCAN_CAP) -> Dict[str, Any]:
    """Row count + per-column cardinality / numeric ranges / sample categories.

    ``scan_cap`` bounds how many rows are scanned for the stats (sum/avg/min/max,
    distinct, top categories). Charts keep the small default; insights pass a
    larger cap so the figures reflect (essentially) the whole result set.
    """
    columns: List[str] = dataset.get("columns") or []
    rows = _iter_rows(dataset)
    row_count = len(rows)
    scan = min(row_count, max(1, scan_cap))

    col_profiles: List[Dict[str, Any]] = []
    for idx, name in enumerate(columns):
        distinct: set = set()
        nulls = numeric_count = date_count = non_null = 0
        cmin = math.inf
        cmax = -math.inf
        csum = 0.0
        for i in range(scan):
            raw = _read_cell(rows[i], name, idx)
            if raw is None or raw == "":
                nulls += 1
                continue
            non_null += 1
            if len(distinct) < 1000:
                distinct.add(_as_label(raw))
            num = _to_number(raw)
            if num is not None:
                numeric_count += 1
                cmin = min(cmin, num)
                cmax = max(cmax, num)
                csum += num
            elif _looks_like_date(raw):
                date_count += 1

        ctype = "category"
        if non_null and numeric_count / non_null > 0.7:
            ctype = "numeric"
        elif non_null and date_count / non_null > 0.7:
            ctype = "date"

        prof: Dict[str, Any] = {
            "name": name, "type": ctype, "distinct": len(distinct), "nulls": nulls,
        }
        if ctype == "numeric" and numeric_count:
            prof.update(
                min=_round(cmin), max=_round(cmax),
                sum=_round(csum), avg=_round(csum / numeric_count),
            )
        else:
            prof["samples"] = list(distinct)[:_MAX_SAMPLE_CATEGORIES]
        col_profiles.append(prof)

    return {"row_count": row_count, "scanned": scan, "columns": col_profiles}


def summarize_profile(profile: Dict[str, Any]) -> str:
    """Render ``profile_dataset`` output into a compact, LLM-friendly text block
    of full-data statistics (one line per column). Used by the insights prompt so
    the model reasons over the WHOLE result set, not just a few sample rows."""
    row_count = profile.get("row_count", 0)
    scanned = profile.get("scanned", row_count)
    head = f"Rows: {row_count}" + ("" if scanned >= row_count else f" (stats over first {scanned})")
    lines = [head]
    for c in profile.get("columns", []):
        name = c.get("name")
        ctype = c.get("type")
        nulls = c.get("nulls", 0)
        if ctype == "numeric":
            lines.append(
                f"- {name} (numeric): min={c.get('min')}, max={c.get('max')}, "
                f"avg={c.get('avg')}, sum={c.get('sum')}, distinct={c.get('distinct')}, nulls={nulls}"
            )
        else:
            samples = c.get("samples") or []
            label = "date" if ctype == "date" else "category"
            extra = ", ".join(str(s) for s in samples[:6])
            lead = "range/examples" if ctype == "date" else "top"
            lines.append(
                f"- {name} ({label}): distinct={c.get('distinct')}, nulls={nulls}"
                + (f", {lead}: {extra}" if extra else "")
            )
    return "\n".join(lines)


# ── aggregation ──────────────────────────────────────────────────────────────

def _reduce(cell: Dict[str, Any], agg: str) -> Optional[float]:
    if not cell:
        return None
    if agg == "avg":
        return cell["sum"] / cell["count"] if cell["count"] else None
    if agg == "min":
        return cell["min"] if math.isfinite(cell["min"]) else None
    if agg == "max":
        return cell["max"] if math.isfinite(cell["max"]) else None
    if agg == "count":
        return cell["rows"]
    if agg == "none":
        return cell["first"]
    return cell["sum"] if cell["count"] else None  # sum (default)


def _aggregate_measure(rows, ctx, y_name, y_index):
    agg = ctx["aggregate"]
    x_name, x_index = ctx["x_name"], ctx["x_index"]
    s_name, s_index = ctx["series_name"], ctx["series_index"]
    x_parts = ctx.get("x_parts")

    categories: List[str] = []
    cat_seen = set()
    series_keys: List[str] = []
    series_seen = set()
    acc: Dict[str, Dict[str, Any]] = {}
    saw_dupe = False
    date_hits = x_non_null = 0

    for row in rows:
        if x_parts:
            x_key = _composite_label(row, x_parts)
            x_raw = x_key
        else:
            x_raw = _read_cell(row, x_name, x_index)
            x_key = _as_label(x_raw)
        if x_key not in cat_seen:
            cat_seen.add(x_key)
            categories.append(x_key)
        if x_raw is not None and x_raw != "":
            x_non_null += 1
            if _looks_like_date(x_raw):
                date_hits += 1

        s_key = _SINGLE
        if s_index >= 0:
            s_key = _as_label(_read_cell(row, s_name, s_index))
        if s_key not in series_seen:
            series_seen.add(s_key)
            series_keys.append(s_key)

        map_key = s_key + "\x00" + x_key
        cell = acc.get(map_key)
        if cell is None:
            cell = {"sum": 0.0, "count": 0, "rows": 0, "min": math.inf, "max": -math.inf, "first": None}
            acc[map_key] = cell
        if cell["rows"] > 0:
            saw_dupe = True
        cell["rows"] += 1

        y_val = 1.0 if agg == "count" else _to_number(_read_cell(row, y_name, y_index))
        if y_val is not None:
            cell["sum"] += y_val
            cell["count"] += 1
            cell["min"] = min(cell["min"], y_val)
            cell["max"] = max(cell["max"], y_val)
            if cell["first"] is None:
                cell["first"] = y_val

    if agg == "none" and saw_dupe:
        agg = "sum"
    # Composite x (e.g. year+month) is inherently time-ordered.
    x_is_date = bool(x_parts) or (bool(x_non_null) and date_hits / x_non_null > 0.7)

    def value_of(s_key: str, cat: str):
        return _reduce(acc.get(s_key + "\x00" + cat), agg)

    return {
        "categories": categories,
        "series_keys": series_keys,
        "x_is_date": x_is_date,
        "value_of": value_of,
    }


def _build_matrix(rows, ctx):
    measures = ctx["y_names"]
    use_group = ctx["series_index"] >= 0
    primary = _aggregate_measure(rows, ctx, measures[0], ctx["y_indexes"][0])
    categories = list(primary["categories"])
    out_series: List[Dict[str, Any]] = []

    if use_group:
        for s_key in primary["series_keys"]:
            out_series.append({
                "name": measures[0] if s_key == _SINGLE else s_key,
                "data": [primary["value_of"](s_key, c) for c in categories],
            })
    else:
        out_series.append({
            "name": measures[0],
            "data": [primary["value_of"](_SINGLE, c) for c in categories],
        })
        for m in range(1, len(measures)):
            agg = _aggregate_measure(rows, ctx, measures[m], ctx["y_indexes"][m])
            out_series.append({
                "name": measures[m],
                "data": [agg["value_of"](_SINGLE, c) for c in categories],
            })

    order = _resolve_order(categories, out_series, ctx, primary["x_is_date"])
    categories = [o[0] for o in order]
    for s in out_series:
        s["data"] = [s["data"][o[1]] for o in order]

    _apply_top_n(categories, out_series, ctx, primary["x_is_date"])
    return {"categories": categories, "series": out_series, "x_is_date": primary["x_is_date"]}


def _resolve_order(categories, series, ctx, x_is_date):
    indexed = list(enumerate(categories))  # (idx, cat)
    indexed = [(cat, idx) for idx, cat in indexed]

    if x_is_date:
        def _ts(item):
            # Real ISO datetimes sort by timestamp; composite labels like
            # "2024-01" fall back to a zero-padded string sort (chronological).
            # The leading 0/1 tag keeps the two key types from being compared.
            try:
                return (0, datetime.fromisoformat(str(item[0])[:19]).timestamp())
            except ValueError:
                return (1, str(item[0]))
        return sorted(indexed, key=_ts)

    want_sort = ctx["sort"] in ("asc", "desc") or ctx["top_n"] > 0
    if not want_sort:
        return indexed

    totals = [sum((s["data"][idx] or 0) for s in series) for idx in range(len(categories))]
    reverse = ctx["sort"] != "asc"
    return sorted(indexed, key=lambda it: totals[it[1]], reverse=reverse)


def _apply_top_n(categories, series, ctx, x_is_date):
    top_n = ctx["top_n"]
    if x_is_date or top_n <= 0 or len(categories) <= top_n:
        return
    removed = categories[top_n:]
    del categories[top_n:]
    if not removed:
        return
    for s in series:
        tail = s["data"][top_n:]
        del s["data"][top_n:]
        s["data"].append(sum((v or 0) for v in tail))
    categories.append(OTHER_LABEL)


# ── formatting ───────────────────────────────────────────────────────────────

def _base_option(spec) -> Dict[str, Any]:
    opt: Dict[str, Any] = {"color": PALETTE, "tooltip": {"trigger": "item"}}
    if spec.get("title"):
        opt["title"] = {"text": spec["title"], "left": "center",
                        "textStyle": {"fontSize": 16, "fontWeight": "normal"}}
    opt["grid"] = {"left": "3%", "right": "4%", "bottom": "8%",
                   "top": 64 if spec.get("title") else 32, "containLabel": True}
    return opt


def _category_axis_label(categories) -> Dict[str, Any]:
    n = len(categories)
    return {"rotate": 35 if n > 8 else 0, "hideOverlap": True,
            "interval": "auto" if n > 30 else 0}


def _legend_for(series):
    return {"type": "scroll", "bottom": 0} if len(series) > 1 else None


# Above this many categories the axis gets a zoom/pan slider so labels stay
# readable and large series render fast.
_ZOOM_THRESHOLD = 30


def _maybe_data_zoom(opt: Dict[str, Any], count: int, horizontal: bool) -> None:
    """Add inside + slider dataZoom (and reserve grid space) when there are many
    categories/points, so the user can zoom and pan."""
    if count <= _ZOOM_THRESHOLD:
        return
    axis_key = "yAxisIndex" if horizontal else "xAxisIndex"
    slider = ({"type": "slider", axis_key: 0, "right": 8, "width": 16}
              if horizontal else
              {"type": "slider", axis_key: 0, "bottom": 8, "height": 16})
    opt["dataZoom"] = [{"type": "inside", axis_key: 0}, slider]
    grid = opt.setdefault("grid", {})
    if horizontal:
        grid["right"] = 72
    else:
        grid["bottom"] = 64


# ── chart-type builders ──────────────────────────────────────────────────────

def _build_cartesian(spec, matrix, kind):
    categories, series = matrix["categories"], matrix["series"]
    horizontal = kind == "horizontal_bar"
    is_line = kind in ("line", "area", "stacked_area")
    is_area = kind in ("area", "stacked_area")
    stacked = spec.get("stacked") or kind in ("stacked_bar", "stacked_area")

    # A genuine, single date column on a line/area chart gets a real TIME axis so
    # irregular gaps are spaced proportionally (composite year+month labels and
    # categorical x stay on a category axis).
    use_time = (is_line and not horizontal and matrix.get("x_is_date")
                and not spec.get("x_parts"))

    if use_time:
        cat_axis = {
            "type": "time",
            "name": spec.get("x_label") or None,
            "axisLabel": {},
        }
    else:
        cat_axis = {
            "type": "category", "data": categories,
            "name": (spec.get("y_label") if horizontal else spec.get("x_label")) or None,
            "boundaryGap": not is_line,
            "axisLabel": {"hideOverlap": True} if horizontal else _category_axis_label(categories),
        }
    val_axis = {
        "type": "value",
        "name": (spec.get("x_label") if horizontal else spec.get("y_label")) or None,
        # Value formatting (compact K/M, currency, percent, decimals) is applied
        # client-side from the jeenFormat hint so it survives chat edits.
        "axisLabel": {},
    }

    opt = _base_option(spec)
    opt["tooltip"] = {"trigger": "axis", "axisPointer": {"type": "line" if is_line else "shadow"}}
    # With several series, order the axis tooltip by value so the biggest
    # contributor is read first.
    if len(series) > 1:
        opt["tooltip"]["order"] = "valueDesc"
    legend = _legend_for(series)
    if legend:
        opt["legend"] = legend
    if horizontal:
        opt["xAxis"], opt["yAxis"] = val_axis, cat_axis
    else:
        opt["xAxis"], opt["yAxis"] = cat_axis, val_axis

    dense = len(categories) > 40
    any_negative = False
    built = []
    for s in series:
        data = s["data"]
        item = {"name": s["name"], "type": "line" if is_line else "bar"}
        if is_line:
            # Time axis needs explicit [x, y] pairs; category axis uses the
            # aligned value list.
            item["data"] = ([[categories[i], data[i]] for i in range(len(data))]
                            if use_time else data)
            item["smooth"] = bool(spec.get("smooth"))
            item["symbol"] = "circle"
            item["symbolSize"] = 0 if dense else 5
            item["showSymbol"] = not dense
            # LTTB downsampling keeps the line's shape fast on long series.
            item["sampling"] = "lttb"
            if is_area:
                item["areaStyle"] = {}
        else:
            # Colour negative bars distinctly unless stacking (where the sign is
            # carried by the stack direction).
            if stacked:
                item["data"] = data
            else:
                item["data"], had_neg = _color_negatives(data)
                any_negative = any_negative or had_neg
            item["barMaxWidth"] = 48
        if stacked:
            item["stack"] = "total"
        item["emphasis"] = {"focus": "series"}
        built.append(item)
    # Make the zero baseline explicit when bars cross it.
    if any_negative and built:
        built[0]["markLine"] = _zero_markline()
    opt["series"] = built
    _maybe_data_zoom(opt, len(categories), horizontal)
    return opt


def _build_pie(spec, matrix, donut):
    categories, series = matrix["categories"], matrix["series"]
    first = series[0] if series else {"data": [], "name": ""}
    data = [{"name": name, "value": first["data"][i]}
            for i, name in enumerate(categories)
            if first["data"][i] is not None]
    opt = _base_option(spec)
    opt["tooltip"] = {"trigger": "item", "formatter": "{b}: {c} ({d}%)"}
    opt["legend"] = {"type": "scroll", "orient": "vertical", "left": "left", "top": "middle"}
    opt["series"] = [{
        "name": spec.get("y_label") or first["name"],
        "type": "pie",
        "radius": ["42%", "68%"] if donut else "62%",
        "center": ["58%", "54%"],
        "data": data,
        # Collapse tiny slivers and keep labels from overlapping.
        "minAngle": 6,
        "avoidLabelOverlap": True,
        "label": {"formatter": "{b}: {d}%"},
        "labelLayout": {"hideOverlap": True},
        "emphasis": {"itemStyle": {"shadowBlur": 10, "shadowColor": "rgba(0,0,0,0.3)"}},
    }]
    return opt


def _build_scatter(spec, rows, ctx):
    x_name, x_index = ctx["x_name"], ctx["x_index"]
    y_name, y_index = ctx["y_names"][0], ctx["y_indexes"][0]
    groups: Dict[str, List[List[float]]] = {}
    for row in rows:
        x = _to_number(_read_cell(row, x_name, x_index))
        y = _to_number(_read_cell(row, y_name, y_index))
        if x is None or y is None:
            continue
        key = _SINGLE
        if ctx["series_index"] >= 0:
            key = _as_label(_read_cell(row, ctx["series_name"], ctx["series_index"]))
        groups.setdefault(key, []).append([x, y])

    opt = _base_option(spec)
    opt["tooltip"] = {"trigger": "item"}
    opt["xAxis"] = {"type": "value", "name": spec.get("x_label") or x_name, "scale": True}
    opt["yAxis"] = {"type": "value", "name": spec.get("y_label") or y_name, "scale": True,
                    "axisLabel": {}}
    # large mode (typed-array rendering) kicks in past largeThreshold points.
    series = [{"name": y_name if k == _SINGLE else k, "type": "scatter",
               "data": pts, "symbolSize": 8, "emphasis": {"focus": "series"},
               "large": True, "largeThreshold": 2000}
              for k, pts in groups.items()]
    legend = _legend_for(series)
    if legend:
        opt["legend"] = legend
    opt["series"] = series
    return opt


def _combo_secondary_names(spec, series) -> set:
    """Names of measures that go on the secondary (right) axis as lines.

    Percent/rate/change measures (e.g. ``yoy_change_pct``) sit on the right axis
    as a line; same-scale measures (e.g. ``revenue_2006``/``revenue_2007``) are
    grouped bars on the left. The LLM may pin this explicitly via ``secondary_y``.
    """
    explicit = {str(n) for n in (spec.get("secondary_y") or [])}
    names = [s["name"] for s in series]
    if explicit:
        secondary = {n for n in names if n in explicit}
    else:
        secondary = {n for n in names if _SECONDARY_MEASURE_RE.search(str(n))}
    # Need bars on at least one axis: if every (or no) measure matched, fall back
    # to "first measure is a bar, the rest are secondary-axis lines".
    if not secondary or len(secondary) >= len(names):
        secondary = set(names[1:])
    return secondary


def _build_combo(spec, matrix):
    categories, series = matrix["categories"], matrix["series"]
    secondary = _combo_secondary_names(spec, series)
    left_name = next((s["name"] for s in series if s["name"] not in secondary), None)
    right_name = next((s["name"] for s in series if s["name"] in secondary), None)

    # Primary (left/bars) format follows the spec; a secondary measure that reads
    # as a percentage gets its OWN percent format on the right axis, so the line
    # shows "34%" while the bars show "$1.3M".
    value_format = spec.get("value_format") or "number"
    symbol = spec.get("currency_symbol") or ""
    primary_scale = (_percent_scale(v for s in series if s["name"] not in secondary
                                    for v in s["data"])
                     if value_format == "percent" else 1)
    primary_meta = _fmt_meta(value_format, symbol, primary_scale)

    def _secondary_meta(name, data):
        if _PERCENT_MEASURE_RE.search(str(name)):
            return _fmt_meta("percent", "", _percent_scale(data))
        return _fmt_meta("number")  # different scale, but not a percentage

    right_meta = next((_secondary_meta(s["name"], s["data"])
                       for s in series if s["name"] in secondary), _fmt_meta("number"))

    opt = _base_option(spec)
    opt["tooltip"] = {"trigger": "axis", "axisPointer": {"type": "cross"},
                      "order": "valueDesc"}
    opt["legend"] = {"type": "scroll", "bottom": 0}
    opt["xAxis"] = {"type": "category", "data": categories,
                    "axisLabel": _category_axis_label(categories)}
    opt["yAxis"] = [
        {"type": "value", "name": left_name, "axisLabel": {}, "jeenFormat": primary_meta},
        {"type": "value", "name": right_name, "splitLine": {"show": False},
         "jeenFormat": right_meta},
    ]

    built = []
    for s in series:
        is_secondary = s["name"] in secondary
        item = {
            "name": s["name"],
            "type": "line" if is_secondary else "bar",
            "yAxisIndex": 1 if is_secondary else 0,
            "data": s["data"],
            "smooth": bool(spec.get("smooth")),
            "barMaxWidth": 48,
            "barGap": "30%",
            "symbolSize": 6,
            "emphasis": {"focus": "series"},
            # Each series formats by its own axis (bars=currency, line=percent).
            "jeenFormat": _secondary_meta(s["name"], s["data"]) if is_secondary else primary_meta,
        }
        # A percent/diff line that dips below zero gets an explicit baseline.
        if is_secondary and any(isinstance(v, (int, float)) and v < 0 for v in s["data"]):
            item["markLine"] = _zero_markline()
        built.append(item)
    opt["series"] = built
    _maybe_data_zoom(opt, len(categories), horizontal=False)
    return opt


def _build_gauge(spec, matrix):
    first = matrix["series"][0]["data"] if matrix["series"] else []
    total = sum((v or 0) for v in first)
    opt = _base_option(spec)
    opt["tooltip"] = {"formatter": "{b}: {c}"}
    opt["series"] = [{
        "type": "gauge", "min": 0, "max": _nice_ceil(total) or 100,
        "progress": {"show": True}, "detail": {"valueAnimation": True, "formatter": "{value}"},
        "data": [{"value": round(total, 2),
                  "name": spec.get("y_label") or (matrix["series"][0]["name"] if matrix["series"] else "")}],
    }]
    return opt


def _build_heatmap(spec, rows, ctx):
    matrix = _build_matrix(rows, ctx)
    x_cats = matrix["categories"]
    y_cats = [s["name"] for s in matrix["series"]]
    data = []
    vmin, vmax = math.inf, -math.inf
    for yi, s in enumerate(matrix["series"]):
        for xi, v in enumerate(s["data"]):
            num = v or 0
            data.append([xi, yi, num])
            vmin, vmax = min(vmin, num), max(vmax, num)
    opt = _base_option(spec)
    opt["tooltip"] = {"position": "top"}
    opt["grid"] = {"left": "3%", "right": "6%", "bottom": "12%",
                   "top": 64 if spec.get("title") else 32, "containLabel": True}
    opt["xAxis"] = {"type": "category", "data": x_cats, "splitArea": {"show": True},
                    "axisLabel": _category_axis_label(x_cats)}
    opt["yAxis"] = {"type": "category", "data": y_cats, "splitArea": {"show": True}}
    opt["visualMap"] = {"min": vmin if math.isfinite(vmin) else 0,
                        "max": vmax if math.isfinite(vmax) else 1,
                        "calculable": True, "orient": "horizontal", "left": "center", "bottom": 0}
    opt["series"] = [{"name": spec.get("title") or "value", "type": "heatmap", "data": data,
                      "label": {"show": len(x_cats) * len(y_cats) <= 100},
                      "emphasis": {"itemStyle": {"shadowBlur": 8, "shadowColor": "rgba(0,0,0,0.4)"}}}]
    return opt


def _map_meta(
    matched: int,
    unmatched: List[str],
    mode: str,
    map_name: str,
    *,
    palette: str = "blue",
    show_labels: bool = False,
    show_unmatched: bool = True,
) -> Dict[str, Any]:
    return {
        "mode": mode,
        "mapName": map_name,
        "matched": matched,
        "unmatched": unmatched[:25],
        "unmatchedCount": len(unmatched),
        "palette": palette,
        "showLabels": show_labels,
        "showUnmatched": show_unmatched,
        "defaultView": _map_view_options(map_name),
    }


def _build_map_choropleth(spec, rows, ctx):
    map_name = spec.get("map_name") or "world"
    defaults = _map_defaults(map_name)
    palette_name, palette = _map_palette(spec)
    show_labels = (
        bool(spec["show_labels"])
        if spec.get("show_labels") is not None
        else bool(defaults.get("showLabels"))
    )
    show_unmatched = spec.get("show_unmatched")
    show_unmatched = True if show_unmatched is None else bool(show_unmatched)
    loc_name = spec.get("location") or ctx["x_name"]
    loc_index = ctx["x_index"] if loc_name == ctx["x_name"] else (
        ctx["columns"].index(loc_name) if loc_name in ctx["columns"] else -1
    )
    value_name = spec.get("value") or ctx["y_names"][0]
    value_index = ctx["columns"].index(value_name) if value_name in ctx["columns"] else ctx["y_indexes"][0]
    agg = ctx["aggregate"]

    acc: Dict[str, Dict[str, Any]] = {}
    unmatched: List[str] = []
    matched_rows = 0
    for row in rows:
        raw_loc = _read_cell(row, loc_name, loc_index)
        canonical = canonical_region(map_name, raw_loc)
        if not canonical:
            label = _as_label(raw_loc)
            if label and label not in unmatched:
                unmatched.append(label)
            continue
        cell = acc.setdefault(
            canonical,
            {"sum": 0.0, "count": 0, "rows": 0, "min": math.inf, "max": -math.inf, "first": None},
        )
        cell["rows"] += 1
        matched_rows += 1
        y_val = 1.0 if agg == "count" else _to_number(_read_cell(row, value_name, value_index))
        if y_val is not None:
            cell["sum"] += y_val
            cell["count"] += 1
            cell["min"] = min(cell["min"], y_val)
            cell["max"] = max(cell["max"], y_val)
            if cell["first"] is None:
                cell["first"] = y_val

    data = [
        {"name": name, "value": _round(value)}
        for name, cell in acc.items()
        if (value := _reduce(cell, agg)) is not None
    ]
    data.sort(key=lambda item: item["value"], reverse=True)
    values = [d["value"] for d in data if isinstance(d.get("value"), (int, float))]
    vmin = min(values) if values else 0
    vmax = max(values) if values else 1

    opt = _base_option(spec)
    opt.pop("grid", None)
    opt["tooltip"] = {"trigger": "item"}
    opt["visualMap"] = {
        "type": "continuous",
        "min": vmin,
        "max": vmax if vmax != vmin else vmin + 1,
        "left": 16,
        "bottom": 18,
        "calculable": True,
        "inRange": {"color": palette},
        "outOfRange": {"color": "#e5e7eb"},
    }
    opt["series"] = [{
        "name": spec.get("y_label") or value_name,
        "type": "map",
        "map": map_name,
        "roam": True,
        **_map_view_options(map_name),
        "data": data,
        "nameProperty": "name",
        "label": {
            "show": show_labels,
            "fontSize": 10,
            "color": "#334155",
            "hideOverlap": True,
        },
        "itemStyle": {
            "borderColor": defaults["borderColor"],
            "borderWidth": defaults["borderWidth"],
            "areaColor": defaults["noDataColor"],
        },
        "emphasis": {"label": {"show": True}, "itemStyle": {"areaColor": palette[-2]}},
        "select": {"label": {"show": True}, "itemStyle": {"areaColor": palette[-1]}},
        "selectedMode": "single",
    }]
    opt["jeenMap"] = _map_meta(
        matched_rows,
        unmatched,
        "choropleth",
        map_name,
        palette=palette_name,
        show_labels=show_labels,
        show_unmatched=show_unmatched,
    )
    return opt


def _point_size(value: float, vmin: float, vmax: float) -> float:
    if not math.isfinite(value):
        return 8
    if vmax <= vmin:
        return 14
    scaled = (value - vmin) / (vmax - vmin)
    return round(8 + 22 * max(0, min(1, scaled)), 2)


def _build_map_points(spec, rows, ctx):
    loc_name = spec.get("location") or ctx["x_name"]
    loc_index = ctx["x_index"] if loc_name == ctx["x_name"] else (
        ctx["columns"].index(loc_name) if loc_name in ctx["columns"] else -1
    )
    lat_name = spec.get("latitude")
    lng_name = spec.get("longitude")
    lat_index = ctx["columns"].index(lat_name) if lat_name in ctx["columns"] else -1
    lng_index = ctx["columns"].index(lng_name) if lng_name in ctx["columns"] else -1
    value_name = spec.get("value") or ctx["y_names"][0]
    value_index = ctx["columns"].index(value_name) if value_name in ctx["columns"] else ctx["y_indexes"][0]
    map_name = spec.get("map_name") or "world"

    raw_points: List[Dict[str, Any]] = []
    unmatched: List[str] = []
    for row in rows:
        label = _as_label(_read_cell(row, loc_name, loc_index)) if loc_index >= 0 else ""
        lat = _to_number(_read_cell(row, lat_name, lat_index)) if lat_index >= 0 else None
        lng = _to_number(_read_cell(row, lng_name, lng_index)) if lng_index >= 0 else None
        if lat is None or lng is None:
            city = lookup_israel_city(label)
            if city:
                lat = _to_number(city.get("lat"))
                lng = _to_number(city.get("lng"))
                label = city.get("name") or label
                map_name = "israel_districts"
            else:
                if label and label not in unmatched:
                    unmatched.append(label)
                continue
        value = _to_number(_read_cell(row, value_name, value_index))
        if value is None:
            value = 1.0 if ctx["aggregate"] == "count" else None
        if value is None:
            continue
        raw_points.append({"name": label or value_name, "lng": lng, "lat": lat, "value": value})

    values = [p["value"] for p in raw_points]
    vmin = min(values) if values else 0
    vmax = max(values) if values else 1
    data = [
        {
            "name": p["name"],
            "value": [_round(p["lng"]), _round(p["lat"]), _round(p["value"])],
            "symbolSize": _point_size(p["value"], vmin, vmax),
        }
        for p in raw_points
    ]

    inferred = infer_map_name(raw_points[0]["name"]) if raw_points else None
    map_name = spec.get("map_name") or inferred or map_name
    defaults = _map_defaults(map_name)
    palette_name, palette = _map_palette(spec)
    show_labels = (
        bool(spec["show_labels"])
        if spec.get("show_labels") is not None
        else bool(defaults.get("showLabels"))
    )
    show_unmatched = spec.get("show_unmatched")
    show_unmatched = True if show_unmatched is None else bool(show_unmatched)
    top_label_names = {
        p["name"]
        for p in sorted(raw_points, key=lambda item: item["value"], reverse=True)[:5]
    } if show_labels else set()
    for item in data:
        if item["name"] in top_label_names:
            item["label"] = {"show": True, "formatter": "{b}", "position": "right"}
    opt = _base_option({**spec, "title": spec.get("title")})
    opt.pop("grid", None)
    opt["tooltip"] = {"trigger": "item"}
    opt["geo"] = {
        "map": map_name,
        "roam": True,
        **_map_view_options(map_name),
        "silent": False,
        "label": {"show": show_labels, "fontSize": 10, "color": "#334155"},
        "itemStyle": {
            "areaColor": defaults["noDataColor"],
            "borderColor": defaults["borderColor"],
            "borderWidth": defaults["borderWidth"],
        },
        "emphasis": {"itemStyle": {"areaColor": palette[1]}},
    }
    opt["visualMap"] = {
        "type": "continuous",
        "min": vmin,
        "max": vmax if vmax != vmin else vmin + 1,
        "left": 16,
        "bottom": 18,
        "dimension": 2,
        "calculable": True,
        "inRange": {"color": [palette[1], palette[-1]]},
    }
    opt["series"] = [{
        "name": spec.get("y_label") or value_name,
        "type": "scatter",
        "coordinateSystem": "geo",
        "data": data,
        "encode": {"value": 2},
        "label": {"show": False, "formatter": "{b}", "position": "right"},
        "labelLayout": {"hideOverlap": True},
        "itemStyle": {"color": palette[-2], "opacity": 0.78, "borderColor": "#ffffff", "borderWidth": 1},
        "emphasis": {"scale": True, "itemStyle": {"opacity": 1}},
    }]
    opt["jeenMap"] = _map_meta(
        len(data),
        unmatched,
        "points",
        map_name,
        palette=palette_name,
        show_labels=show_labels,
        show_unmatched=show_unmatched,
    )
    return opt


def _build_map(spec, rows, ctx):
    mode = spec.get("map_mode") or "choropleth"
    if mode == "points":
        return _build_map_points(spec, rows, ctx)
    return _build_map_choropleth(spec, rows, ctx)


def _new_point_cell(label: str, lat: float, lng: float) -> Dict[str, Any]:
    return {
        "label": label,
        "lat": lat,
        "lng": lng,
        "rows": 0,
        "rowIndexes": [],
        "primary": {"sum": 0.0, "count": 0, "rows": 0, "min": math.inf, "max": -math.inf, "first": None},
        "secondary": {"sum": 0.0, "count": 0, "rows": 0, "min": math.inf, "max": -math.inf, "first": None},
    }


def _add_point_value(cell: Dict[str, Any], value: Optional[float], key: str) -> None:
    bucket = cell[key]
    bucket["rows"] += 1
    if value is None:
        return
    bucket["sum"] += value
    bucket["count"] += 1
    bucket["min"] = min(bucket["min"], value)
    bucket["max"] = max(bucket["max"], value)
    if bucket["first"] is None:
        bucket["first"] = value


def _point_reduce(bucket: Dict[str, Any], aggregate: str) -> Optional[float]:
    # `none` is only meaningful for a single row. Duplicate coordinates must
    # collapse deterministically, matching the existing chart aggregation rule.
    effective = "sum" if aggregate == "none" and bucket["rows"] > 1 else aggregate
    return _reduce(bucket, effective)


def _build_osm_map(spec, rows, ctx):
    """Build a renderer-neutral raster-basemap + point-overlay payload."""
    loc_name = spec.get("location") or ctx["x_name"]
    loc_index = ctx["x_index"] if loc_name == ctx["x_name"] else (
        ctx["columns"].index(loc_name) if loc_name in ctx["columns"] else -1
    )
    lat_name = spec.get("latitude")
    lng_name = spec.get("longitude")
    lat_index = ctx["columns"].index(lat_name) if lat_name in ctx["columns"] else -1
    lng_index = ctx["columns"].index(lng_name) if lng_name in ctx["columns"] else -1
    value_name = spec.get("value") or ctx["y_names"][0]
    value_index = ctx["columns"].index(value_name) if value_name in ctx["columns"] else ctx["y_indexes"][0]
    value2_name = spec.get("value2")
    value2_index = ctx["columns"].index(value2_name) if value2_name in ctx["columns"] else -1
    location_parts = spec.get("location_parts")
    location_parts = location_parts if isinstance(location_parts, dict) else {}
    location_part_columns = [
        location_parts.get(role)
        for role in ("place", "admin1", "country", "postal")
        if location_parts.get(role) in ctx["columns"]
    ]
    location_part_indexes = {
        column: ctx["columns"].index(column) for column in location_part_columns
    }
    resolved_locations = spec.get("resolved_locations")
    resolved_locations = resolved_locations if isinstance(resolved_locations, dict) else {}
    aggregate = ctx["aggregate"]

    grouped: Dict[tuple[float, float], Dict[str, Any]] = {}
    unmatched: List[str] = []
    unmatched_by_status: Dict[str, set[str]] = {}
    for row_index, row in enumerate(rows):
        label_parts = [
            _as_label(_read_cell(row, column, location_part_indexes[column]))
            for column in location_part_columns
        ]
        label_parts = [part for part in label_parts if part]
        label = ", ".join(label_parts) or (
            _as_label(_read_cell(row, loc_name, loc_index)) if loc_index >= 0 else ""
        )
        resolution_key = location_key("|".join(label_parts)) or location_key(label)
        lat = _to_number(_read_cell(row, lat_name, lat_index)) if lat_index >= 0 else None
        lng = _to_number(_read_cell(row, lng_name, lng_index)) if lng_index >= 0 else None
        if not valid_coordinates(lat, lng) or (lat == 0 and lng == 0):
            city = lookup_israel_city(label)
            resolved = resolved_locations.get(resolution_key)
            if city and valid_coordinates(city.get("lat"), city.get("lng")):
                lat, lng = float(city["lat"]), float(city["lng"])
                label = city.get("name") or label
            elif isinstance(resolved, dict) and resolved.get("status") == "resolved" and valid_coordinates(
                resolved.get("lat"), resolved.get("lng")
            ):
                lat, lng = float(resolved["lat"]), float(resolved["lng"])
            else:
                if label and label not in unmatched:
                    unmatched.append(label)
                status = (
                    str(resolved.get("status") or "unmatched")
                    if isinstance(resolved, dict)
                    else "unmatched"
                )
                if status == "unresolved":
                    status = "unmatched"
                unmatched_by_status.setdefault(status, set()).add(resolution_key or label)
                continue

        primary = 1.0 if aggregate == "count" else _to_number(_read_cell(row, value_name, value_index))
        if primary is None:
            continue
        secondary = _to_number(_read_cell(row, value2_name, value2_index)) if value2_index >= 0 else None
        point_key = (round(float(lat), 7), round(float(lng), 7))
        point = grouped.get(point_key)
        if point is None:
            point = _new_point_cell(label or value_name, point_key[0], point_key[1])
            grouped[point_key] = point
        point["rows"] += 1
        point["rowIndexes"].append(row_index)
        _add_point_value(point, primary, "primary")
        _add_point_value(point, secondary, "secondary")

    points = []
    for point in grouped.values():
        value = _point_reduce(point["primary"], aggregate)
        value2 = _point_reduce(point["secondary"], aggregate) if value2_name else None
        if value is None:
            continue
        points.append(
            {
                "label": point["label"],
                "lat": _round(point["lat"]),
                "lng": _round(point["lng"]),
                "value": _round(value),
                "value2": _round(value2) if value2 is not None else None,
                "rowCount": point["rows"],
                "rowIndexes": point["rowIndexes"][:200],
                "placeKey": location_key(point["label"]),
            }
        )

    primary_values = [point["value"] for point in points]
    secondary_values = [point["value2"] for point in points if point["value2"] is not None]
    latitudes = [point["lat"] for point in points]
    longitudes = [point["lng"] for point in points]
    show_unmatched = spec.get("show_unmatched")
    show_unmatched = True if show_unmatched is None else bool(show_unmatched)
    map_layers = browser_map_layers()
    map_layers["dataLayers"] = [{
        "id": "user-data",
        "label": f"{value_name} data",
        "kind": "data",
        "defaultVisible": True,
    }]

    return {
        # Maintains the existing chart response shape while telling ChartManager
        # to use the dedicated OSM renderer instead of ECharts.
        "series": [],
        "jeenOsmMap": {
            "basemap": {
                "type": "raster",
                "tileUrl": "/api/map-tiles/{z}/{x}/{y}",
                "attribution": "© OpenStreetMap contributors",
            },
            "layers": map_layers,
            "dataLayerMode": spec.get("data_layer_mode") or "auto",
            "overlays": [
                {
                    "type": "circles",
                    "palette": spec.get("map_palette") or "blue",
                    "metric": value_name,
                    "sizeMetric": value2_name or value_name,
                    "aggregate": aggregate,
                    "points": points,
                    "colorRange": {
                        "min": _round(min(primary_values)) if primary_values else 0,
                        "max": _round(max(primary_values)) if primary_values else 1,
                    },
                    "sizeRange": {
                        "min": _round(min(secondary_values or primary_values)) if (secondary_values or primary_values) else 0,
                        "max": _round(max(secondary_values or primary_values)) if (secondary_values or primary_values) else 1,
                    },
                }
            ],
            "extent": {
                "minLat": _round(min(latitudes)) if latitudes else None,
                "maxLat": _round(max(latitudes)) if latitudes else None,
                "minLng": _round(min(longitudes)) if longitudes else None,
                "maxLng": _round(max(longitudes)) if longitudes else None,
            },
            "matched": len(points),
            "rowCount": sum(point["rowCount"] for point in points),
            "unmatchedCount": len(unmatched),
            "unmatched": unmatched[:20],
            "unmatchedByStatus": {
                status: len(keys) for status, keys in unmatched_by_status.items()
            },
            "showUnmatched": show_unmatched,
        },
    }


# ── entry point ──────────────────────────────────────────────────────────────

def build_chart_option(spec: Dict[str, Any], dataset: Dict[str, Any]) -> Dict[str, Any]:
    """Build an ECharts option from a validated spec and the full result set.

    Returns a pure-JSON dict (no callables) ready to ship to the browser.
    """
    if not isinstance(spec, dict):
        raise ValueError("Missing chart spec")
    columns: List[str] = dataset.get("columns") or []
    rows = _iter_rows(dataset)
    if not columns or not rows:
        raise ValueError("No data to chart")

    # Cap the rows we actually plot (a >5k-point categorical chart is unreadable);
    # aggregation usually collapses well below this anyway.
    if len(rows) > _PLOT_ROW_CAP:
        rows = rows[:_PLOT_ROW_CAP]

    def index_of(name: Optional[str]) -> int:
        return columns.index(name) if name in columns else -1

    raw_y = spec.get("y")
    y_list = raw_y if isinstance(raw_y, list) else [raw_y]
    is_virtual_row_count = str(spec.get("value") or "") == "__row_count__"
    y_names = [n for n in y_list if isinstance(n, str) and n in columns]
    if is_virtual_row_count and not y_names:
        y_names = ["__row_count__"]
    if not y_names:
        raise ValueError("Spec has no valid measure column")

    # Composite x-axis (e.g. separate year + month columns joined into one label).
    raw_parts = spec.get("x_parts")
    x_parts = []
    if isinstance(raw_parts, list):
        for nm in raw_parts:
            if isinstance(nm, str) and nm in columns:
                x_parts.append((nm, columns.index(nm)))
    x_parts = x_parts if len(x_parts) >= 2 else None

    ctx = {
        "columns": columns,
        "x_name": spec.get("x"),
        "x_index": index_of(spec.get("x")),
        "x_parts": x_parts,
        "y_names": y_names,
        "y_indexes": [index_of(n) for n in y_names],
        "series_name": spec.get("series"),
        "series_index": index_of(spec.get("series")),
        "aggregate": spec.get("aggregate") or "sum",
        "sort": spec.get("sort") or "none",
        "top_n": spec["top_n"] if isinstance(spec.get("top_n"), int) and spec["top_n"] > 0 else 0,
    }

    ctype = str(spec.get("chart_type") or "bar").lower()
    if ctype == "map":
        opt = _build_map(spec, rows, ctx)
    elif ctype == "osm_map":
        opt = _build_osm_map(spec, rows, ctx)
    elif ctype == "pie":
        opt = _build_pie(spec, _build_matrix(rows, ctx), donut=False)
    elif ctype == "donut":
        opt = _build_pie(spec, _build_matrix(rows, ctx), donut=True)
    elif ctype == "scatter":
        opt = _build_scatter(spec, rows, ctx)
    elif ctype == "combo":
        opt = _build_combo(spec, _build_matrix(rows, ctx))
    elif ctype == "gauge":
        opt = _build_gauge(spec, _build_matrix(rows, ctx))
    elif ctype == "heatmap":
        opt = (_build_heatmap(spec, rows, ctx) if ctx["series_index"] >= 0
               else _build_cartesian(spec, _build_matrix(rows, ctx), "bar"))
    elif ctype in ("horizontal_bar", "line", "area", "stacked_area", "stacked_bar"):
        opt = _build_cartesian(spec, _build_matrix(rows, ctx), ctype)
    else:
        opt = _build_cartesian(spec, _build_matrix(rows, ctx), "bar")

    # Value-formatting hint consumed client-side (compact K/M, currency, percent,
    # decimals). Carried in the option so it survives chat edits; the client
    # strips it before ECharts setOption. `symbol` is empty unless the currency
    # is actually known — we never assume "$". For percent, `scale` (×100) is
    # added when the data is stored as 0–1 fractions so it reads as 0–100%.
    value_format = spec.get("value_format") or "number"
    symbol = (spec.get("currency_symbol") or "") if value_format == "currency" else ""
    scale = _percent_scale(_option_values(opt)) if value_format == "percent" else 1
    opt["jeenFormat"] = _fmt_meta(value_format, symbol, scale)
    return opt


def _option_values(opt: Dict[str, Any]):
    """Yield the numeric measure values from a built option (handles plain
    numbers, {value,…} items, and [x, y] pairs)."""
    osm = opt.get("jeenOsmMap")
    if isinstance(osm, dict):
        for overlay in osm.get("overlays") or []:
            if not isinstance(overlay, dict):
                continue
            for point in overlay.get("points") or []:
                value = point.get("value") if isinstance(point, dict) else None
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    yield value
    for s in opt.get("series", []) or []:
        for v in (s.get("data") or []):
            if isinstance(v, dict):
                v = v.get("value")
            if isinstance(v, list):
                v = v[-1] if v else None
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                yield v
