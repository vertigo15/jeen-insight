"""Chart endpoints: initial generation, enhancement, and chat-driven edits."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Response

from src.api.chart_builder import build_chart_option, profile_dataset
from src.api.dependencies import get_history_service, require_user_id, resolve_agent
from src.api.llm_json import (
    extract_chart_type,
    extract_json_object,
    normalise_derived_series,
)
from src.api.llm_params import (
    EDIT_CHART_PARAMS,
    ENHANCE_CHART_PARAMS,
    GENERATE_CHART_PARAMS,
)
from src.api.map_geocoding import (
    chart_capabilities,
    geocoding_enabled,
    infer_geo_roles,
    osm_maps_enabled,
    resolve_osm_locations,
    search_osm_places,
)
from src.api.models import (
    ChatMessage,
    DerivedSeriesSpec,
    EditChartRequest,
    EditChartResponse,
    EnhanceChartRequest,
    GenerateChartRequest,
    GenerateChartResponse,
)
from src.api.result_cache import result_cache
from src.config import settings

logger = logging.getLogger(__name__)
# httpx logs full request URLs at INFO. Tile and MapTiler geocoder URLs carry
# their credential as a query parameter, so do not emit those URLs to app logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
router = APIRouter(prefix="/api", tags=["charts"])


@router.get("/chart-capabilities")
async def get_chart_capabilities():
    """Expose UI-safe chart feature flags without leaking provider secrets."""
    return chart_capabilities()


@router.get("/map-tiles/{z}/{x}/{y}")
async def proxy_map_tile(z: int, x: int, y: int):
    """Proxy configured raster tiles so tile credentials never reach the browser."""
    if not osm_maps_enabled():
        raise HTTPException(status_code=404, detail="OpenStreetMap maps are not enabled")
    if not 0 <= z <= 19 or not 0 <= x < 2**z or not 0 <= y < 2**z:
        raise HTTPException(status_code=422, detail="Invalid map tile coordinates")

    template = settings.OSM_TILE_URL.strip()
    if not all(token in template for token in ("{z}", "{x}", "{y}")):
        logger.error("OSM_TILE_URL must contain {z}, {x}, and {y} placeholders")
        raise HTTPException(status_code=503, detail="Map tiles are misconfigured")

    url = (
        template.replace("{z}", str(z))
        .replace("{x}", str(x))
        .replace("{y}", str(y))
        .replace("{api_key}", settings.OSM_TILE_API_KEY)
    )
    headers: dict[str, str] = {}
    if settings.OSM_TILE_API_KEY and "{api_key}" not in template:
        headers[settings.OSM_TILE_API_KEY_HEADER.strip() or "Authorization"] = settings.OSM_TILE_API_KEY

    try:
        async with httpx.AsyncClient(
            timeout=max(0.1, float(settings.OSM_TILE_TIMEOUT_SECONDS)),
            follow_redirects=False,
        ) as client:
            upstream = await client.get(url, headers=headers)
        upstream.raise_for_status()
    except httpx.HTTPError:
        logger.warning("Configured map tile provider request failed")
        raise HTTPException(status_code=502, detail="Map tile provider is unavailable") from None

    content_type = upstream.headers.get("content-type", "image/png").split(";")[0]
    return Response(
        content=upstream.content,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/map-search")
async def map_search(q: str = Query(min_length=2, max_length=200)):
    """Proxy managed place search for map navigation without exposing an API key."""
    if not geocoding_enabled():
        raise HTTPException(status_code=503, detail="Place search is not configured")
    return {"results": await search_osm_places(q)}


async def _verify_query_owner(*, query_id: Optional[str], user_id: str, connection: str) -> None:
    if not query_id:
        return
    history = get_history_service()
    if not await history.query_belongs_to_user(
        query_id=query_id, user_id=user_id, source_key=connection
    ):
        raise HTTPException(status_code=404, detail="Query not found for this user")


# ----------------------------------------------------------------------
# Chart-edit (chart chat) prompt + budgets
# ----------------------------------------------------------------------
_CHART_EDITOR_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "agent"
    / "prompts"
    / "chart_editor.md"
)
_CHART_EDITOR_MAX_INSTRUCTION_CHARS = 500
_CHART_EDITOR_MAX_RECENT_MESSAGES = 6
_CHART_EDITOR_MAX_RECENT_CHARS = 1500


def _load_chart_editor_prompt() -> str:
    """Re-read the externalised prompt on every call so editing the .md file
    has zero deploy cost in dev."""
    return _CHART_EDITOR_PROMPT_PATH.read_text(encoding="utf-8")


def _format_recent_messages(messages: Optional[List[ChatMessage]]) -> str:
    if not messages:
        return "(none)"
    trimmed = messages[-_CHART_EDITOR_MAX_RECENT_MESSAGES:]
    lines: List[str] = []
    used = 0
    for m in trimmed:
        role = m.role if m.role in ("user", "assistant") else "user"
        content = (m.content or "").strip()
        if not content:
            continue
        line = f"[{role}] {content}"
        if used + len(line) > _CHART_EDITOR_MAX_RECENT_CHARS:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines) or "(none)"


# ----------------------------------------------------------------------
# Visualization-spec contract for initial chart generation
#
# The LLM no longer transcribes data values into an ECharts config (which only
# ever saw a 10-row sample and could miscopy numbers). Instead it returns a
# compact SPEC describing the encoding; the client builds the option from the
# FULL result set. See static/chart-feature/utils/chartSpecBuilder.js.
# ----------------------------------------------------------------------
_ALLOWED_CHART_TYPES = {
    "bar", "line", "area", "pie", "donut", "scatter", "horizontal_bar",
    "stacked_bar", "stacked_area", "combo", "heatmap", "gauge", "map", "osm_map",
}
_ALLOWED_AGGREGATES = {"sum", "avg", "count", "min", "max", "none"}
_ALLOWED_SORTS = {"asc", "desc", "none"}
_ALLOWED_FORMATS = {"number", "currency", "percent", "none"}
_ALLOWED_MAP_MODES = {"choropleth", "points"}
_ALLOWED_MAP_NAMES = {"world", "world_detailed", "israel_districts"}
_ALLOWED_MAP_QUALITIES = {"standard", "detailed"}
_ALLOWED_MAP_PALETTES = {"blue", "green", "purple", "orange"}
_ALLOWED_MAP_FOCUS = {"world", "israel", "auto"}


def _profile_blob(profile: dict) -> str:
    """Render a server-computed data profile into compact prompt text."""
    cols = profile.get("columns") if isinstance(profile, dict) else None
    row_count = profile.get("row_count") if isinstance(profile, dict) else None

    lines: list[str] = []
    if row_count is not None:
        lines.append(f"Total rows: {row_count}")

    lines.append("Columns:")
    for c in cols or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        ctype = c.get("type", "?")
        distinct = c.get("distinct")
        bits = [f"distinct={distinct}"] if distinct is not None else []
        if c.get("type") == "numeric":
            bits.append(
                f"min={c.get('min')} max={c.get('max')} "
                f"avg={c.get('avg')} sum={c.get('sum')}"
            )
        elif c.get("samples"):
            sample = ", ".join(str(s) for s in c.get("samples", [])[:8])
            bits.append(f"e.g. {sample}")
        lines.append(f"- {name} ({ctype}) — " + ", ".join(bits))

    return "\n".join(lines)


_GENERATE_SYSTEM_PROMPT = (
    "You are a senior data-visualization expert. Given a dataset's schema and a\n"
    "statistical profile, choose the SINGLE best chart and return a compact JSON\n"
    "SPEC describing how to encode it. You DO NOT draw the chart or echo data\n"
    "values — the application renders the full dataset from your spec and handles\n"
    "all number formatting. Decide the encoding; the app builds it.\n\n"
    "Return ONLY valid JSON (no markdown fences, no comments, no prose). Schema:\n"
    "{\n"
    '  "chart_type": "bar|line|area|pie|donut|scatter|horizontal_bar|stacked_bar|stacked_area|combo|heatmap|gauge|map|osm_map",\n'
    '  "x": "<column for the category or time axis (pie/donut label dimension)>",\n'
    '  "x_parts": ["<col>", "<col>"]  // OPTIONAL: 2+ columns to join into one ordered axis label, e.g. ["year","month"]. Omit or null otherwise.,\n'
    '  "y": ["<one or more numeric measure columns>"],\n'
    '  "secondary_y": ["<subset of y to draw on a right-hand axis as a line; combo only>"]  // OPTIONAL,\n'
    '  "series": "<column to split into multiple series/segments, or null>",\n'
    '  "aggregate": "sum|avg|count|min|max|none",\n'
    '  "sort": "asc|desc|none",\n'
    '  "top_n": <integer or null>,\n'
    '  "title": "<concise human title>",\n'
    '  "x_label": "<axis label or null>",\n'
    '  "y_label": "<axis label or null>",\n'
    '  "value_format": "number|currency|percent|none",\n'
    '  "currency_symbol": "<currency symbol like $, €, £, ₪ — ONLY if the currency is known; else null>",\n'
    '  "map_mode": "choropleth|points|null",\n'
    '  "map_name": "world|world_detailed|israel_districts|null",\n'
    '  "location": "<country/region/district/city column for map charts, or null>",\n'
    '  "location_parts": {"place":"<city/place>", "admin1":"<state/province>", "country":"<country>", "postal":"<postal code>"}  // OPTIONAL for compound point-map geocoding,\n'
    '  "latitude": "<latitude column for point maps, or null>",\n'
    '  "longitude": "<longitude/lng column for point maps, or null>",\n'
    '  "value": "<numeric measure column for map charts, __row_count__ for a location count, or null>",\n'
    '  "value2": "<optional second numeric measure for OpenStreetMap point size, or null>",\n'
    '  "map_quality": "standard|detailed|null",\n'
    '  "map_palette": "blue|green|purple|orange|null",\n'
    '  "show_labels": <true|false|null>,\n'
    '  "show_unmatched": <true|false|null>,\n'
    '  "map_focus": "world|israel|auto|null",\n'
    '  "stacked": <true|false>,\n'
    '  "smooth": <true|false>,\n'
    '  "reason": "<one short sentence>"\n'
    "}\n\n"
    "CHOOSING THE BEST CHART (follow these viz best practices):\n"
    "- TIME / ORDERED x (a date column, OR separate year/month/quarter columns) →\n"
    "  LINE (use area only for volume/cumulative magnitude). Time is continuous,\n"
    "  so a line shows the trend; do NOT use a pie/donut for time.\n"
    "- DISCRETE categories compared by a measure → BAR. If labels are long or there\n"
    "  are many categories (>12) → horizontal_bar with sort=desc and top_n (~15).\n"
    "- Parts of a whole, few categories (≤6) → pie or donut. Never a pie for >8\n"
    "  slices or for time — use bar/line instead.\n"
    "- One measure split by a second category → stacked_bar / stacked_area\n"
    "  (set series and stacked=true).\n"
    "- Correlation between two numeric measures → scatter (x and y both numeric).\n"
    "- Single headline KPI → gauge. Two categorical dims + one measure → heatmap.\n\n"
    "MAPS / GEOGRAPHY:\n"
    "- If the dataset has a country/region/district/location column plus a numeric\n"
    "  measure, you may choose chart_type \"map\" with map_mode \"choropleth\".\n"
    "- If the dataset has latitude and longitude columns plus a numeric measure,\n"
    "  choose chart_type \"map\" with map_mode \"points\".\n"
    "- For Israeli district-level data, use map_name \"israel_districts\" and\n"
    "  map_mode \"choropleth\". For Israeli city data with lat/lng or known city\n"
    "  names, use map_name \"israel_districts\" and map_mode \"points\".\n"
    "- For country-level data, use map_name \"world\".\n"
    "- Use map_quality \"detailed\" only when the user asks for a higher quality\n"
    "  map; otherwise use \"standard\" or null. Use map_palette only for style\n"
    "  requests. Use show_labels=true for small district maps or top city points.\n"
    "- Never invent coordinates. If city names have no lat/lng and are not clearly\n"
    "  Israeli city names, prefer horizontal_bar instead of a map.\n\n"
    "WIDE / PERIOD-COMPARISON DATA (e.g. revenue_2006 vs revenue_2007):\n"
    "- When the SAME metric is split across columns by period/group (revenue_2006,\n"
    "  revenue_2007; sales_q1..q4; this_year/last_year), put ALL those columns in y\n"
    "  so they render as GROUPED BARS — do NOT chart just one of them.\n"
    "- If a change/percentage/difference column is also present (e.g. yoy_change_pct,\n"
    "  growth, delta), use chart_type \"combo\": keep the period columns in y as bars\n"
    "  and list the change/% column in BOTH y and secondary_y so it draws as a line\n"
    "  on a second right-hand axis. This is the classic bars + diff-line view.\n\n"
    "DATES & TIME AXES:\n"
    "- A real date/timestamp column → use it as x with chart_type line; the app\n"
    "  sorts chronologically automatically.\n"
    "- SEPARATE year & month (or year & quarter) columns → set x_parts:[\"year\",\"month\"]\n"
    "  (year first) and chart_type line. The app joins them into ordered labels\n"
    "  like \"2024-01\" and sorts them in time order. Do NOT put month on x and year\n"
    "  on series for a single trend line.\n\n"
    "VALUE FORMATTING:\n"
    "- Set value_format by the measure's MEANING: currency for money/sales/revenue,\n"
    "  percent for rates/ratios/shares, number otherwise.\n"
    "- CURRENCY: do NOT assume US dollars. Set currency_symbol ONLY when the data\n"
    "  actually tells you the currency — e.g. a column named amount_usd/price_eur,\n"
    "  a currency/iso code column, or symbols present in the sample values. If the\n"
    "  currency is unknown, keep value_format=currency but leave currency_symbol\n"
    "  null; the app then shows a plain number with no symbol.\n"
    "- Do NOT pre-scale or round values and do NOT add K/M/$/%% yourself. The app\n"
    "  abbreviates large numbers (1.2K, 3.4M, 1.1B) and picks sensible decimals.\n\n"
    "MORE VIZ BEST PRACTICES:\n"
    "- Keep it to ONE message: pick the single most relevant measure for y unless a\n"
    "  combo/stack is clearly needed. Avoid >2 measures on one chart.\n"
    "- Limit series: if splitting by `series` would create many lines/segments\n"
    "  (>~6), instead set top_n (~15) on x and drop series, or keep the few biggest.\n"
    "- Bars encode magnitude from a zero baseline — never start a bar's value axis\n"
    "  above zero. Lines may use a fitted range to show trend.\n"
    "- Use combo for measures with different units/scales (e.g. revenue as bars +\n"
    "  margin %% as line, or two period columns as bars + their %% change as line);\n"
    "  otherwise prefer a single type.\n"
    "- Don't put high-cardinality IDs/keys (order id, customer id) on x — aggregate\n"
    "  to a meaningful category or time instead.\n"
    "- For ranking questions (top/bottom/most/least) use horizontal_bar + sort=desc.\n\n"
    "IDENTIFIERS ARE NOT MEASURES:\n"
    "- Numeric columns that are really labels/ordinals — month_number, year, quarter,\n"
    "  week, day, rank, *_id, *_number — are DIMENSIONS. Never put them in y. Use\n"
    "  them on x (or to order/label x), e.g. month_number orders the months but the\n"
    "  measure on y is revenue/sales, not the month number itself.\n\n"
    "RULES:\n"
    "- x, x_parts[], y[], secondary_y[] and series MUST be exact column names.\n"
    "- y must be numeric MEASURES (values you'd sum/average), not id/ordinal columns;\n"
    "  aggregate when x (and series) repeats. Prefer sum for additive quantities and\n"
    "  avg for rates/ratios/prices.\n"
    "- Sort categorical charts by the measure desc unless x is time (chronological).\n"
    "- Use the sample rows ONLY to understand shape/meaning, never to copy values."
)

_OSM_MAP_PROMPT = (
    "\n\nOPENSTREETMAP POINT MAPS:\n"
    "- `osm_map` is enabled for this deployment. Choose it only for point-level "
    "data with latitude/longitude, or a city/place location column.\n"
    "- Use `value` for marker color. With a second meaningful numeric measure, "
    "set `value2` to encode marker radius; otherwise omit it.\n"
    "- Prefer a detected latitude/longitude pair when it has high coverage. If "
    "using names, set `location_parts` with place plus state/province and country "
    "when available; never geocode an IP address or postal code alone.\n"
    "- When a raw geography result has no real measure, use value `__row_count__` "
    "and aggregate `count`; never use a key/code/identifier as a map measure.\n"
    "- Prefer the existing Flat Map (`map`) for country, region, or district "
    "choropleths. Never use `osm_map` for a high-cardinality identifier.\n"
)


def _generation_system_prompt() -> str:
    """Advertise OSM only when the backend is configured to render it."""
    if not osm_maps_enabled():
        return _GENERATE_SYSTEM_PROMPT
    geocoder_note = (
        "Configured place-name geocoding is available."
        if geocoding_enabled()
        else "Only existing coordinates and bundled local city lookups are available; "
        "do not select `osm_map` for other place names."
    )
    return _GENERATE_SYSTEM_PROMPT + _OSM_MAP_PROMPT + f"\n- {geocoder_note}\n"


def _coerce_columns(value, lowered: dict[str, str]) -> list[str]:
    """Map LLM-provided column names back to their canonical spelling, dropping
    anything that isn't a real column."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        canon = lowered.get(item.strip().lower())
        if canon and canon not in out:
            out.append(canon)
    return out


def _columns_from_profile(profile: dict) -> tuple[list[str], list[str], list[str]]:
    """Derive (column_names, numeric_cols, date_cols) from a data profile."""
    column_names: list[str] = []
    numeric_cols: list[str] = []
    date_cols: list[str] = []
    for c in (profile.get("columns") or []):
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if not name:
            continue
        column_names.append(name)
        ctype = c.get("type")
        if ctype == "numeric":
            numeric_cols.append(name)
        elif ctype == "date":
            date_cols.append(name)
    return column_names, numeric_cols, date_cols


# Numeric columns whose name ends in an identifier/ordinal token (month_number,
# year, order_id, …) are dimensions, not measures — never auto-pick them for y.
_IDENTIFIER_RE = re.compile(
    r"(id|key|code|number|no|num|year|month|day|quarter|qtr|week|"
    r"rank|index|idx|seq|postal|zip|ip|locator)s?$",
    re.I,
)
_LATITUDE_RE = re.compile(r"(^|_)(lat|latitude)$", re.I)
_LONGITUDE_RE = re.compile(r"(^|_)(lon|lng|long|longitude)$", re.I)
_LOCATION_RE = re.compile(
    r"(country|nation|region|district|state|province|city|town|municipality|locality|location)",
    re.I,
)
_GEO_HINT_PATTERNS = {
    "latitude": re.compile(r"\b(latitude|lat)\b", re.I),
    "longitude": re.compile(r"\b(longitude|lon|lng|long)\b", re.I),
    "place": re.compile(r"\b(city|town|municipality|locality|location|place)\b", re.I),
    "admin1": re.compile(r"\b(state|province|region|district)\b", re.I),
    "country": re.compile(r"\b(country|nation)\b", re.I),
    "postal": re.compile(r"\b(postal(?:\s+code)?|zip)\b", re.I),
}


def _looks_like_identifier(name: str) -> bool:
    return bool(_IDENTIFIER_RE.search(name or ""))


def _first_name_matching(columns: list[str], pattern: re.Pattern) -> Optional[str]:
    return next((c for c in columns if pattern.search(c or "")), None)


def _geo_hints_from_metadata_columns(
    metadata_columns: list[dict[str, Any]], column_names: list[str]
) -> dict[str, str]:
    """Extract advisory geo roles from curated column descriptions.

    Descriptions may be authored in a consistent form such as
    ``Geographic role: latitude (WGS84)``.  They are hints only: callers still
    validate that latitude/longitude values are numeric and in range.
    """
    result_by_name = {name.casefold(): name for name in column_names}
    hints: dict[str, str] = {}
    for column in metadata_columns:
        if not isinstance(column, dict):
            continue
        result_name = result_by_name.get(str(column.get("column") or "").casefold())
        description = column.get("description")
        if not result_name or not isinstance(description, str):
            continue
        for role, pattern in _GEO_HINT_PATTERNS.items():
            if role not in hints and pattern.search(description):
                hints[role] = result_name
    return hints


async def _load_geo_hints(connection: str, column_names: list[str]) -> dict[str, str]:
    """Read cached Jeen Metadata descriptions without making charts depend on it."""
    from src.api import state

    loader = state.metadata_loader
    if loader is None or not connection:
        return {}
    try:
        columns = await loader.load_columns(connection)
    except Exception:  # noqa: BLE001
        logger.debug("Chart geo hints unavailable from metadata", exc_info=True)
        return {}
    return _geo_hints_from_metadata_columns(columns, column_names)


def _geo_hints_blob(hints: dict[str, str]) -> str:
    if not hints:
        return ""
    lines = [
        f"- {role}: {column}"
        for role, column in sorted(hints.items())
    ]
    return (
        "\n\nCatalog geographic hints (advisory; use exact column names and do not "
        "invent coordinates):\n" + "\n".join(lines)
    )


def _geo_roles_blob(roles: dict[str, Any]) -> str:
    """Serialize only inferred column names/coverage, never raw place values."""
    candidates = roles.get("candidates") if isinstance(roles, dict) else {}
    pairs = roles.get("coordinate_pairs") if isinstance(roles, dict) else []
    lines = []
    for role in ("place", "admin1", "country", "postal", "latitude", "longitude"):
        columns = candidates.get(role) if isinstance(candidates, dict) else None
        if columns:
            lines.append(f"- {role}: {', '.join(columns)}")
    for pair in pairs[:3] if isinstance(pairs, list) else []:
        if isinstance(pair, dict):
            lines.append(
                f"- coordinate pair: {pair.get('latitude')} + {pair.get('longitude')} "
                f"({int(float(pair.get('coverage', 0)) * 100)}% valid)"
            )
    if not lines:
        return ""
    return (
        "\n\nDetected geographic candidates (use only these exact columns; "
        "never use identifiers, IP addresses, or postal codes as a measure):\n"
        + "\n".join(lines)
    )


def _validate_chart_spec(
    spec: dict,
    *,
    column_names: list[str],
    numeric_cols: list[str],
    date_cols: list[str],
    forced_type: Optional[str] = None,
    x_col: Optional[str] = None,
    y_col: Optional[str] = None,
    series_col: Optional[str] = None,
    location_col: Optional[str] = None,
    latitude_col: Optional[str] = None,
    longitude_col: Optional[str] = None,
    location_parts_override: Optional[dict[str, str]] = None,
    value_col: Optional[str] = None,
    value2_col: Optional[str] = None,
    aggregate_override: Optional[str] = None,
    geo_hints: Optional[dict[str, str]] = None,
    geo_roles: Optional[dict[str, Any]] = None,
    osm_enabled: bool = True,
) -> dict:
    """Clamp the LLM spec to safe, real values and apply user overrides.

    Guarantees: valid chart_type, an x dimension, ≥1 numeric measure, and
    enumerations limited to the allowed sets. Falls back to sensible defaults
    derived from the detected column types when the LLM is vague or wrong.
    """
    lowered = {c.lower(): c for c in column_names}
    numeric_set = set(numeric_cols)
    non_numeric = [c for c in column_names if c not in numeric_set]
    geo_hints = geo_hints or {}
    geo_roles = geo_roles or {}
    geo_candidates = geo_roles.get("candidates") if isinstance(geo_roles, dict) else {}
    geo_candidates = geo_candidates if isinstance(geo_candidates, dict) else {}
    coordinate_pairs = geo_roles.get("coordinate_pairs") if isinstance(geo_roles, dict) else []
    coordinate_pairs = coordinate_pairs if isinstance(coordinate_pairs, list) else []

    def geo_candidate(role: str) -> Optional[str]:
        hinted = geo_hints.get(role)
        if hinted in column_names:
            return hinted
        candidates = geo_candidates.get(role)
        if isinstance(candidates, list):
            return next((column for column in candidates if column in column_names), None)
        return None

    if not isinstance(spec, dict):
        spec = {}

    chart_type = str(spec.get("chart_type", "")).strip().lower()
    if chart_type not in _ALLOWED_CHART_TYPES:
        chart_type = "bar"

    # x dimension
    x_list = _coerce_columns(spec.get("x"), lowered)
    x = x_list[0] if x_list else None
    if x is None:
        x = (date_cols[0] if date_cols else None) or (
            non_numeric[0] if non_numeric else (column_names[0] if column_names else None)
        )

    # y measures. Identifier/ordinal numerics (month_number, year, *_id) are
    # dimensions, so we never default to them — and if the model picked ONLY
    # such columns while real measures exist, we swap in the real measures.
    real_measures = [c for c in numeric_cols if not _looks_like_identifier(c)]
    y = [c for c in _coerce_columns(spec.get("y"), lowered) if c in numeric_set]
    if y and real_measures and all(_looks_like_identifier(c) for c in y):
        y = real_measures
    if not y:
        y = real_measures[:1] or numeric_cols[:1] or ([c for c in column_names if c != x][:1])

    # Map-specific fields. These live alongside x/y so the rest of the API can
    # still display selected columns, but map building does not rely on a
    # cartesian axis interpretation.
    map_mode = str(spec.get("map_mode", "")).strip().lower()
    if map_mode not in _ALLOWED_MAP_MODES:
        map_mode = ""

    map_name = str(spec.get("map_name", "")).strip().lower()
    if map_name not in _ALLOWED_MAP_NAMES:
        map_name = ""

    map_quality = str(spec.get("map_quality", "")).strip().lower()
    if map_quality not in _ALLOWED_MAP_QUALITIES:
        map_quality = "standard"

    map_palette = str(spec.get("map_palette", "")).strip().lower()
    if map_palette not in _ALLOWED_MAP_PALETTES:
        map_palette = "blue"

    map_focus = str(spec.get("map_focus", "")).strip().lower()
    if map_focus not in _ALLOWED_MAP_FOCUS:
        map_focus = "auto"

    show_labels = spec.get("show_labels")
    show_labels = show_labels if isinstance(show_labels, bool) else None
    show_unmatched = spec.get("show_unmatched")
    show_unmatched = show_unmatched if isinstance(show_unmatched, bool) else True

    location_cols = _coerce_columns(spec.get("location"), lowered)
    location = location_cols[0] if location_cols else None
    latitude_cols = _coerce_columns(spec.get("latitude"), lowered)
    latitude = latitude_cols[0] if latitude_cols else None
    longitude_cols = _coerce_columns(spec.get("longitude"), lowered)
    longitude = longitude_cols[0] if longitude_cols else None
    value_cols = [c for c in _coerce_columns(spec.get("value"), lowered) if c in numeric_set]
    value = value_cols[0] if value_cols else (y[0] if y else None)
    value2_cols = [c for c in _coerce_columns(spec.get("value2"), lowered) if c in numeric_set]
    value2 = value2_cols[0] if value2_cols else next((c for c in y if c != value), None)
    raw_location_parts = spec.get("location_parts")
    location_parts = {}
    if isinstance(raw_location_parts, dict):
        for role in ("place", "admin1", "country", "postal"):
            column = raw_location_parts.get(role)
            if isinstance(column, str) and column in column_names:
                location_parts[role] = column

    if not location:
        location = geo_candidate("place") or _first_name_matching(non_numeric, _LOCATION_RE)
        if not location and chart_type != "osm_map":
            location = x
    if not latitude:
        latitude = geo_candidate("latitude") or _first_name_matching(numeric_cols, _LATITUDE_RE)
    if not longitude:
        longitude = geo_candidate("longitude") or _first_name_matching(numeric_cols, _LONGITUDE_RE)
    if not location_parts:
        for role in ("place", "admin1", "country", "postal"):
            column = geo_candidate(role)
            if column:
                location_parts[role] = column
        if location and "place" not in location_parts:
            location_parts["place"] = location
    if location_parts.get("place"):
        location = location_parts["place"]
    if coordinate_pairs:
        strongest_pair = coordinate_pairs[0]
        if (
            isinstance(strongest_pair, dict)
            and float(strongest_pair.get("coverage", 0)) >= 0.5
        ):
            latitude = latitude or strongest_pair.get("latitude")
            longitude = longitude or strongest_pair.get("longitude")

    if forced_type and forced_type not in ("auto", "", None):
        forced = forced_type.strip().lower()
        if forced in _ALLOWED_CHART_TYPES:
            chart_type = forced

    if chart_type == "osm_map" and not osm_enabled:
        chart_type = "horizontal_bar"

    if chart_type == "map":
        if not map_mode:
            map_mode = "points" if latitude and longitude else "choropleth"
        if not map_name:
            if map_focus == "israel":
                map_name = "israel_districts"
            elif map_focus == "world":
                map_name = "world"
            elif location and re.search(r"(district|city|town|municipality)", location, re.I):
                map_name = "israel_districts"
            else:
                map_name = "world"
        if map_quality == "detailed" and map_name == "world":
            map_name = "world_detailed"

        can_render_points = bool(value and location) or bool(value and latitude and longitude)
        can_render_choropleth = bool(value and location)
        if map_mode == "points" and not can_render_points:
            chart_type = "horizontal_bar"
            map_mode = ""
        elif map_mode == "choropleth" and not can_render_choropleth:
            chart_type = "horizontal_bar"
            map_mode = ""
        elif value:
            y = [value]
            if location:
                x = location

    if chart_type == "osm_map":
        can_render_points = bool(value and latitude and longitude) or bool(value and location)
        if not can_render_points:
            chart_type = "horizontal_bar"
        else:
            y = [value]
            if value2 and value2 != value:
                y.append(value2)
            if location:
                x = location

    # series (group-by) — must differ from x
    series_list = _coerce_columns(spec.get("series"), lowered)
    series = next((c for c in series_list if c != x), None)

    # Combo: measures to draw on the secondary (right) y-axis as a line. Only
    # meaningful for combo charts; filtered to the final y measures below.
    secondary_raw = [c for c in _coerce_columns(spec.get("secondary_y"), lowered) if c in numeric_set]

    # Composite x-axis: e.g. separate year + month columns joined into one
    # ordered time label. Only honoured when ≥2 real columns are named.
    x_parts = _coerce_columns(spec.get("x_parts"), lowered)
    x_parts = x_parts if len(x_parts) >= 2 else None

    aggregate = str(spec.get("aggregate", "")).strip().lower()
    if aggregate not in _ALLOWED_AGGREGATES:
        aggregate = "sum"
    if aggregate_override and aggregate_override.strip().lower() in _ALLOWED_AGGREGATES:
        aggregate = aggregate_override.strip().lower()

    sort = str(spec.get("sort", "")).strip().lower()
    if sort not in _ALLOWED_SORTS:
        sort = "none"

    top_n = spec.get("top_n")
    if not isinstance(top_n, int) or top_n <= 0:
        top_n = None

    value_format = str(spec.get("value_format", "")).strip().lower()
    if value_format not in _ALLOWED_FORMATS:
        value_format = "number"

    # Currency symbol is only meaningful for currency, and only when the model
    # could actually identify the currency. We never assume "$".
    currency_symbol = spec.get("currency_symbol")
    if value_format == "currency" and isinstance(currency_symbol, str):
        currency_symbol = currency_symbol.strip()[:4]
    else:
        currency_symbol = ""

    title = spec.get("title")
    if not isinstance(title, str) or not title.strip():
        title = f"{y[0]} by {x}" if y and x else "Chart"

    def _label(key):
        v = spec.get(key)
        return v.strip()[:60] if isinstance(v, str) and v.strip() else None

    # ── User overrides from the column-mapping panel (MUST win) ──────────
    if x_col and x_col in column_names:
        x = x_col
        if chart_type in ("map", "osm_map"):
            location = x_col
        x_parts = None  # explicit single-column choice overrides a composite axis
    if y_col and y_col in column_names:
        y = [y_col]
        if chart_type in ("map", "osm_map"):
            value = y_col
    if location_col and location_col in column_names:
        location = location_col
        location_parts["place"] = location_col
    if latitude_col and latitude_col in numeric_set:
        latitude = latitude_col
    if longitude_col and longitude_col in numeric_set:
        longitude = longitude_col
    if isinstance(location_parts_override, dict):
        for role in ("place", "admin1", "country", "postal"):
            column = location_parts_override.get(role)
            if isinstance(column, str) and column in column_names:
                location_parts[role] = column
        if location_parts.get("place"):
            location = location_parts["place"]
    if value_col == "__row_count__":
        value = "__row_count__"
    elif value_col and value_col in numeric_set:
        value = value_col
    if value2_col and value2_col in numeric_set and value2_col != value:
        value2 = value2_col
    elif value2_col == "":
        value2 = None
    if series_col is not None:
        series = series_col if series_col in column_names else None
    if series == x:
        series = None
    # A composite part must not double as the series split.
    if x_parts and series in x_parts:
        series = None
    if chart_type == "osm_map":
        if not value or _looks_like_identifier(value):
            value = real_measures[0] if real_measures else "__row_count__"
        if value == "__row_count__":
            aggregate = "count"
            if not isinstance(spec.get("title"), str) or not spec.get("title", "").strip():
                title = f"Location count by {location or x}"
        y = [value] if value else []
        if value2 and value2 != value:
            y.append(value2)

    return {
        "chart_type": chart_type,
        "x": x,
        "x_parts": x_parts,
        "y": y,
        "secondary_y": [c for c in secondary_raw if c in set(y)],
        "series": series,
        "aggregate": aggregate,
        "sort": sort,
        "top_n": top_n,
        "title": title.strip()[:120],
        "x_label": _label("x_label"),
        "y_label": _label("y_label"),
        "value_format": value_format,
        "currency_symbol": currency_symbol,
        "map_mode": map_mode or None,
        "map_name": map_name or None,
        "location": location,
        "location_parts": location_parts,
        "latitude": latitude,
        "longitude": longitude,
        "value": value,
        "value2": value2 if chart_type == "osm_map" and value2 != value else None,
        "geo_roles": geo_roles,
        "map_quality": map_quality,
        "map_palette": map_palette,
        "show_labels": show_labels,
        "show_unmatched": show_unmatched,
        "map_focus": map_focus,
        "stacked": bool(spec.get("stacked")),
        "smooth": bool(spec.get("smooth")),
        "reason": (spec.get("reason") or "").strip()[:200]
        if isinstance(spec.get("reason"), str) else "",
    }


def _dataset_from_request(request: GenerateChartRequest) -> Optional[dict]:
    """Reconstruct a {columns, rows} dataset from the client-sent fallback
    payload (all_data + column_names). Returns None when no rows were sent."""
    rows = request.all_data
    cols = request.column_names
    if rows and cols:
        return {"columns": list(cols), "rows": rows}
    return None


# ----------------------------------------------------------------------
# Initial chart generation
#
# Flow: LLM *decides* (a compact spec); Python *builds* the ECharts option from
# the FULL result set. The rows come from the server-side result cache (keyed by
# user+connection+query_id); on a cache miss we ask the client to re-send them
# (HTTP 409), then build from those. The chart therefore always reflects every
# row, and the LLM never transcribes values.
# ----------------------------------------------------------------------
@router.post("/generate-chart", response_model=GenerateChartResponse)
async def generate_chart(request: GenerateChartRequest):
    user_id = require_user_id(request.user_id)
    await _verify_query_owner(
        query_id=request.query_id, user_id=user_id, connection=request.connection
    )
    agent = await resolve_agent(request.connection)
    chart_type_param = (request.chart_type or "auto").strip().lower()
    if chart_type_param == "osm_map" and not osm_maps_enabled():
        raise HTTPException(
            status_code=422,
            detail="OpenStreetMap maps are not configured for this deployment.",
        )

    # 1. Resolve the dataset: cache first, then client-sent fallback.
    dataset = result_cache.get(
        user_id=user_id,
        connection=request.connection,
        query_id=request.query_id,
    )
    if dataset is None:
        dataset = _dataset_from_request(request)
    if dataset is None:
        # Cache miss and no rows in the body — ask the client to re-send them.
        raise HTTPException(status_code=409, detail="cache_miss")

    # 2. Profile the FULL dataset server-side; derive column metadata from it.
    profile = profile_dataset(dataset)
    column_names, numeric_cols, date_cols = _columns_from_profile(profile)
    if not column_names:
        raise HTTPException(
            status_code=422,
            detail="Could not determine chartable columns for this result set.",
        )
    geo_hints = await _load_geo_hints(request.connection, column_names)
    geo_roles = infer_geo_roles(dataset, geo_hints)

    # 3. Build the decision prompt (profile + intent + user overrides).
    instruction_parts: list[str] = []
    if request.question and request.question.strip():
        instruction_parts.append(
            f'User question that produced this data: "{request.question.strip()[:300]}". '
            "Prefer a chart that answers it."
        )
    if chart_type_param not in ("auto", ""):
        instruction_parts.append(
            f'The user explicitly selected chart_type "{chart_type_param}". You MUST use it.'
        )
    mapping: list[str] = []
    if request.x_column:
        mapping.append(f"x = {request.x_column}")
    if request.y_column:
        mapping.append(f"y = {request.y_column}")
    if request.series_column:
        mapping.append(f"series = {request.series_column}")
    for label, value in (
        ("place", request.location_column),
        ("latitude", request.latitude_column),
        ("longitude", request.longitude_column),
        ("value", request.value_column),
        ("size value", request.value2_column),
        ("aggregate", request.aggregate),
    ):
        if value:
            mapping.append(f"{label} = {value}")
    if request.location_parts:
        mapping.append(
            "location parts = "
            + ", ".join(f"{role}:{column}" for role, column in request.location_parts.items())
        )
    if mapping:
        instruction_parts.append(
            "User-selected column mapping (MUST follow): " + "; ".join(mapping)
        )
    instruction_blob = ("\n\n" + "\n".join(instruction_parts)) if instruction_parts else ""

    sample = list((dataset.get("rows") or [])[:50])
    user_prompt = (
        "Choose the best chart for this dataset and return the JSON spec.\n\n"
        f"Dataset profile (computed over ALL {profile.get('row_count', 0)} rows):\n"
        f"{_profile_blob(profile)}{_geo_hints_blob(geo_hints)}"
        f"{_geo_roles_blob(geo_roles)}{instruction_blob}\n\n"
        f"Sample rows (first {len(sample)} of {profile.get('row_count', 0)}, for "
        "SHAPE/MEANING ONLY — do not copy these values into the chart):\n"
        + json.dumps(sample, indent=2, default=str)
        + "\n\nReturn ONLY the JSON spec."
    )

    try:
        response = await agent.llm.generate(
            messages=[
                {"role": "system", "content": _generation_system_prompt()},
                {"role": "user", "content": user_prompt},
            ],
            temperature=GENERATE_CHART_PARAMS.temperature,
            max_tokens=GENERATE_CHART_PARAMS.max_tokens,
        )
        raw = response.get("content") or ""
        parsed = extract_json_object(raw)
        if parsed is None:
            logger.error(
                "Chart-spec LLM response was not parseable JSON. First 500 chars: %s",
                raw[:500],
            )
            # Degrade gracefully to a validated default spec rather than erroring.
            parsed = {}

        spec = _validate_chart_spec(
            parsed,
            column_names=column_names,
            numeric_cols=numeric_cols,
            date_cols=date_cols,
            forced_type=request.chart_type,
            x_col=request.x_column,
            y_col=request.y_column,
            series_col=request.series_column,
            location_col=request.location_column,
            latitude_col=request.latitude_column,
            longitude_col=request.longitude_column,
            location_parts_override=request.location_parts,
            value_col=request.value_column,
            value2_col=request.value2_column,
            aggregate_override=request.aggregate,
            geo_hints=geo_hints,
            geo_roles=geo_roles,
            osm_enabled=osm_maps_enabled(),
        )
        if not spec["x"] or not spec["y"]:
            raise HTTPException(
                status_code=422,
                detail="Could not determine chartable columns for this result set.",
            )
        if spec["chart_type"] == "osm_map":
            spec["resolved_locations"] = await resolve_osm_locations(spec, dataset)

        # 4. Build the actual ECharts option from the FULL dataset.
        try:
            chart_config = build_chart_option(spec, dataset)
        except Exception as build_err:  # noqa: BLE001
            logger.exception("Chart build failed for spec %s", spec)
            raise HTTPException(
                status_code=500, detail=f"Chart build failed: {build_err}"
            ) from build_err

        return GenerateChartResponse(
            chart_config=chart_config,
            chart_type=spec["chart_type"],
            chart_spec=spec,
            prompt=user_prompt,
            system_message=_generation_system_prompt(),
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Chart generation error")
        raise HTTPException(status_code=500, detail=f"Chart generation failed: {e}") from e


# ----------------------------------------------------------------------
# Chart chat: per-session, natural-language edits
# ----------------------------------------------------------------------
@router.post("/edit-chart", response_model=EditChartResponse)
async def edit_chart(request: EditChartRequest):
    """Apply a natural-language edit to the current ECharts config.

    The endpoint never touches the SQL result set. It returns a new chart
    config (potentially identical to the input on out-of-scope requests)
    plus an optional list of `derived_series` specs that the client computes
    from the existing dataset.
    """
    instruction = (request.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="`instruction` is required")
    if not request.current_config:
        raise HTTPException(status_code=400, detail="`current_config` is required")

    agent = await resolve_agent(request.connection)
    instruction = instruction[:_CHART_EDITOR_MAX_INSTRUCTION_CHARS]

    column_types_blob = (
        "\n".join(f"- {c.name} ({c.type})" for c in request.columns) or "(unknown)"
    )
    sample_blob = json.dumps(request.sample_data[:5], ensure_ascii=False, indent=2)
    config_blob = json.dumps(request.current_config, ensure_ascii=False)
    column_names_blob = json.dumps(request.column_names, ensure_ascii=False)
    recent_blob = _format_recent_messages(request.recent_messages)

    from src.api import state as app_state
    if app_state.prompt_cache:
        try:
            template        = await app_state.prompt_cache.get_content("chart_editor")
            model_override  = await app_state.prompt_cache.get_model_override("chart_editor")
        except Exception:
            template       = _load_chart_editor_prompt()
            model_override = None
    else:
        template       = _load_chart_editor_prompt()
        model_override = None

    try:
        system_prompt = template.format(
            instruction=instruction,
            column_names=column_names_blob,
            column_types=column_types_blob,
            sample_rows=sample_blob,
            current_config=config_blob,
            recent_messages=recent_blob,
        )
    except (KeyError, IndexError, ValueError):
        logger.exception("Failed to format chart_editor prompt")
        raise HTTPException(status_code=500, detail="Chart editor prompt is malformed")

    try:
        response = await agent.llm.generate(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": instruction},
            ],
            temperature=EDIT_CHART_PARAMS.temperature,
            max_tokens=EDIT_CHART_PARAMS.max_tokens,
            model_override=model_override,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Chart edit LLM call failed")
        return EditChartResponse(
            chart_config=request.current_config,
            chart_type=extract_chart_type(request.current_config),
            derived_series=[],
            notes=f"Sorry, the chart-edit service is unavailable right now ({e}).",
            out_of_scope=True,
            prompt=system_prompt,
            system_message=None,
        )

    raw = response.get("content") or ""
    parsed = extract_json_object(raw)
    if not isinstance(parsed, dict):
        logger.warning("Chart-edit LLM returned unparseable JSON (%d chars)", len(raw))
        return EditChartResponse(
            chart_config=request.current_config,
            chart_type=extract_chart_type(request.current_config),
            derived_series=[],
            notes="I couldn't apply that edit. Please rephrase, or try one of the suggestions.",
            out_of_scope=True,
            prompt=system_prompt,
        )

    out_of_scope = bool(parsed.get("out_of_scope"))
    chart_config = parsed.get("chart_config")
    if not isinstance(chart_config, dict):
        chart_config = request.current_config
        out_of_scope = True

    # If the user asked to change value formatting, carry the hint into the
    # config so the client applies it (compact K/M, currency, percent).
    jeen_format = parsed.get("jeenFormat")
    if isinstance(jeen_format, dict) and jeen_format.get("kind") in _ALLOWED_FORMATS:
        kind = jeen_format["kind"]
        symbol = jeen_format.get("symbol")
        symbol = symbol.strip()[:4] if (kind == "currency" and isinstance(symbol, str)) else ""
        chart_config["jeenFormat"] = {
            "kind": kind,
            "compact": bool(jeen_format.get("compact", True)),
            "symbol": symbol,
        }

    derived = normalise_derived_series(
        parsed.get("derived_series"), request.column_names
    )

    chart_type = parsed.get("chart_type")
    if not isinstance(chart_type, str) or not chart_type.strip():
        chart_type = extract_chart_type(chart_config)

    notes = parsed.get("notes")
    if isinstance(notes, str):
        notes = notes.strip()[:300] or None
    else:
        notes = None

    return EditChartResponse(
        chart_config=chart_config,
        chart_type=chart_type,
        derived_series=[DerivedSeriesSpec(**d) for d in derived],
        notes=notes,
        out_of_scope=out_of_scope,
        prompt=system_prompt,
    )


# ----------------------------------------------------------------------
# One-shot enhancement of an existing chart config
# ----------------------------------------------------------------------
@router.post("/enhance-chart")
async def enhance_chart_endpoint(request: EnhanceChartRequest):
    agent = await resolve_agent(request.connection)
    system_prompt = (
        "You are a data visualization expert specializing in Apache ECharts. "
        "Enhance the provided basic ECharts config: meaningful title, smart "
        "number formatting (K/M/B), better colors, clear axis labels, polished "
        "tooltips. Return ONLY valid JSON, no markdown fences, no explanations."
    )
    user_prompt = (
        f"Enhance this {request.chart_type} chart configuration.\n\n"
        "Column Information:\n"
        + "\n".join(f"- {c.name} ({c.type})" for c in request.columns)
        + "\n\nSample Data (first few rows):\n"
        + json.dumps(request.sample_data[:5], indent=2)
        + "\n\nCurrent Basic Configuration:\n"
        + json.dumps(request.current_config, indent=2)
        + "\n\nReturn ONLY the JSON configuration, no other text."
    )
    try:
        response = await agent.llm.generate(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=ENHANCE_CHART_PARAMS.temperature,
            max_tokens=ENHANCE_CHART_PARAMS.max_tokens,
        )
        raw = response.get("content") or ""
        enhanced_config = extract_json_object(raw)
        if enhanced_config is None or not isinstance(enhanced_config, dict):
            logger.error(
                "Enhance-chart LLM response was not parseable JSON. First 500 chars: %s",
                raw[:500],
            )
            raise HTTPException(
                status_code=500,
                detail="LLM did not return valid JSON for the chart enhancement.",
            )
        return {"enhanced_config": enhanced_config}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Chart enhancement error")
        raise HTTPException(status_code=500, detail=f"Chart enhancement failed: {e}") from e
