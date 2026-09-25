"""Chart endpoints: initial generation, enhancement, and chat-driven edits."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Union

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ValidationError

from src.agent.conversation_artifacts import measure_chart_payload
from src.api.chart_builder import build_chart_option, profile_dataset
from src.api.chart_edit_validation import validate_chart_edit
from src.api.chart_operations import (
    CHART_OPERATION_SAFETY_CONTRACT,
    OPERATION_ENVELOPE_ADAPTER,
    OperationContractError,
    validate_operation_envelope,
)
from src.api.dependencies import get_history_service, get_principal, resolve_agent
from src.security.internal_auth import Principal
from src.api.llm_json import (
    extract_chart_type,
    extract_json_object,
    normalise_derived_series,
)
from src.api.llm_params import (
    EDIT_CHART_OPERATIONS_PARAMS,
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
from src.api.map_layers import browser_map_layers, configured_map_layers, valid_tile_template
from src.api.map_tile_cache import map_tile_cache
from src.api.models import (
    ChatMessage,
    ChartEditError,
    DerivedSeriesSpec,
    EditChartRebuildRequest,
    EditChartRebuildResponse,
    EditChartRequest,
    EditChartResponse,
    EditChartV2Response,
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


def _tile_cache_control() -> str:
    """Browser cache window matches the server tile TTL, with a short stale window."""
    max_age = max(0, int(settings.OSM_TILE_CACHE_TTL_SECONDS))
    stale = min(86400, max(600, max_age // 7))
    return f"private, max-age={max_age}, stale-while-revalidate={stale}"


def _if_none_match(request: Request, etag: str) -> bool:
    incoming = (request.headers.get("if-none-match") or "").strip()
    return bool(etag and incoming == etag)


def _tile_response_headers(
    started_at: float,
    *,
    etag: str = "",
    last_modified: str = "",
    cache_status: str,
) -> dict[str, str]:
    headers = {
        "Cache-Control": _tile_cache_control(),
        "Server-Timing": f"maptile;dur={round((time.monotonic() - started_at) * 1000)}",
        "X-Cache": cache_status,
    }
    if etag:
        headers["ETag"] = etag
    if last_modified:
        headers["Last-Modified"] = last_modified
    return headers


async def _proxy_map_tile(layer_id: str, z: int, x: int, y: int, request: Request):
    """Proxy one approved raster layer without exposing provider credentials."""
    started_at = time.monotonic()
    if not osm_maps_enabled():
        raise HTTPException(status_code=404, detail="OpenStreetMap maps are not enabled")
    if not 0 <= z <= 19 or not 0 <= x < 2**z or not 0 <= y < 2**z:
        raise HTTPException(status_code=422, detail="Invalid map tile coordinates")

    layer = configured_map_layers().get(layer_id)
    if layer is None:
        raise HTTPException(status_code=404, detail="Map layer is unavailable")
    template = layer.source_template
    if not valid_tile_template(template):
        logger.error("Map tile layer is misconfigured: %s", layer_id)
        raise HTTPException(status_code=503, detail="Map tiles are misconfigured")

    cached = map_tile_cache.get(layer_id, z, x, y)
    if cached:
        response_headers = _tile_response_headers(
            started_at,
            etag=cached.etag,
            last_modified=cached.last_modified,
            cache_status="HIT",
        )
        if _if_none_match(request, cached.etag):
            return Response(status_code=304, headers=response_headers)
        return Response(
            content=cached.content,
            media_type=cached.content_type,
            headers=response_headers,
        )

    url = (
        template.replace("{z}", str(z))
        .replace("{x}", str(x))
        .replace("{y}", str(y))
        .replace("{api_key}", layer.api_key)
    )
    headers: dict[str, str] = {}
    if layer.api_key and "{api_key}" not in template:
        headers[layer.api_key_header.strip() or "Authorization"] = layer.api_key

    try:
        client = getattr(request.app.state, "map_tile_client", None)
        if client is None:
            async with httpx.AsyncClient(
                timeout=max(0.1, float(settings.OSM_TILE_TIMEOUT_SECONDS)),
                follow_redirects=False,
            ) as transient_client:
                upstream = await transient_client.get(url, headers=headers)
        else:
            upstream = await client.get(url, headers=headers)
        upstream.raise_for_status()
    except httpx.HTTPError:
        logger.warning(
            "osm_tile_proxy layer=%s status=provider_error elapsed_ms=%d",
            layer_id,
            round((time.monotonic() - started_at) * 1000),
        )
        raise HTTPException(status_code=502, detail="Map tile provider is unavailable") from None

    content_type = upstream.headers.get("content-type", "image/png").split(";")[0]
    etag = upstream.headers.get("ETag") or ""
    last_modified = upstream.headers.get("Last-Modified") or ""
    map_tile_cache.put(
        layer_id,
        z,
        x,
        y,
        content=upstream.content,
        content_type=content_type,
        etag=etag,
        last_modified=last_modified,
    )
    response_headers = _tile_response_headers(
        started_at,
        etag=etag,
        last_modified=last_modified,
        cache_status="MISS",
    )
    logger.info(
        "osm_tile_proxy layer=%s status=200 cache=miss elapsed_ms=%d bytes=%d",
        layer_id,
        round((time.monotonic() - started_at) * 1000),
        len(upstream.content),
    )
    return Response(
        content=upstream.content,
        media_type=content_type,
        headers=response_headers,
    )


@router.get("/map-tiles/{layer_id}/{z}/{x}/{y}")
async def proxy_configured_map_tile(layer_id: str, z: int, x: int, y: int, request: Request):
    """Proxy a named, server-approved base or overlay raster layer."""
    return await _proxy_map_tile(layer_id, z, x, y, request)


@router.get("/map-tiles/{z}/{x}/{y}")
async def proxy_map_tile(z: int, x: int, y: int, request: Request):
    """Backward-compatible proxy for the configured Standard raster layer."""
    return await _proxy_map_tile("standard", z, x, y, request)


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


async def _chart_request_watermark() -> Optional[datetime]:
    """Use PostgreSQL's clock for cross-replica chart write ordering."""
    history = get_history_service()
    method = getattr(history, "get_chart_request_watermark", None)
    if not callable(method):
        return None
    value = await method()
    return value if isinstance(value, datetime) else None


async def _persist_chart_baseline(
    *,
    query_id: Optional[str],
    user_id: str,
    chart_spec: Optional[dict],
    chart_config: Optional[dict],
    request_started_at: Optional[datetime] = None,
) -> None:
    """Best-effort: store the server-built chart for conversation restore.

    Skipped when persistence is off, when the request has no query_id (nothing
    to attach to), or when the payload exceeds CONVERSATION_CHART_MAX_BYTES.
    Never fails the chart response.
    """
    if not query_id or not chart_config:
        return
    try:
        history = get_history_service()
    except HTTPException:
        return
    if getattr(history, "persistence_enabled", False) is not True:
        return
    try:
        from uuid import UUID as _UUID

        turn_id = _UUID(str(query_id))
        size = measure_chart_payload(chart_spec, chart_config)
        if size > settings.CONVERSATION_CHART_MAX_BYTES:
            logger.info(
                "conversation_chart_skipped query_id=%s bytes=%d cap=%d",
                query_id, size, settings.CONVERSATION_CHART_MAX_BYTES,
                extra={"event": "conversation_chart_skipped", "bytes": size},
            )
            # The user now sees this (unpersisted) chart; an older stored
            # baseline would be restored in its place, so drop it.
            await history.clear_turn_chart(
                turn_id=turn_id,
                user_id=user_id,
                request_started_at=request_started_at,
            )
            return

        await history.upsert_turn_chart(
            turn_id=turn_id,
            user_id=user_id,
            chart_spec=chart_spec,
            chart_config=chart_config,
            chart_bytes=size,
            request_started_at=request_started_at,
        )
    except Exception:  # noqa: BLE001
        logger.debug("chart baseline persistence failed", exc_info=True)


# ----------------------------------------------------------------------
# Chart-edit (chart chat) prompt + budgets
# ----------------------------------------------------------------------
_CHART_EDITOR_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "agent"
    / "prompts"
    / "chart_editor.md"
)
_MAP_CHART_EDITOR_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "agent"
    / "prompts"
    / "chart_map_editor.md"
)
_CHART_EDITOR_MAX_INSTRUCTION_CHARS = 500
# Upper bound for the single chart-edit model call; the browser gives up on a
# refinement that takes longer than a conversational pause.
EDIT_CHART_LLM_TIMEOUT_SECONDS = 8
_CHART_EDITOR_MAX_RECENT_MESSAGES = 6
_CHART_EDITOR_MAX_RECENT_CHARS = 1500


def _load_chart_editor_prompt() -> str:
    """Re-read the externalised prompt on every call so editing the .md file
    has zero deploy cost in dev."""
    return _CHART_EDITOR_PROMPT_PATH.read_text(encoding="utf-8")


def _load_map_chart_editor_prompt() -> str:
    """Load the constrained map-edit prompt outside the ECharts-only editor."""
    return _MAP_CHART_EDITOR_PROMPT_PATH.read_text(encoding="utf-8")


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
    # ML-skill result chart (role-based; built by /api/analysis/chart, never the LLM).
    "band",
}
_ALLOWED_AGGREGATES = {"sum", "avg", "count", "min", "max", "none"}
_ALLOWED_SORTS = {"asc", "desc", "none"}
_ALLOWED_FORMATS = {"number", "currency", "percent", "none"}
_ALLOWED_MAP_MODES = {"choropleth", "points"}
_ALLOWED_MAP_NAMES = {"world", "world_detailed", "israel_districts"}
_ALLOWED_MAP_QUALITIES = {"standard", "detailed"}
_ALLOWED_MAP_PALETTES = {"blue", "green", "purple", "orange"}
_ALLOWED_MAP_FOCUS = {"world", "israel", "auto"}
_ALLOWED_OSM_DATA_LAYER_MODES = {"auto", "points", "clusters"}


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
    data_layer_mode = str(spec.get("data_layer_mode", "")).strip().lower()
    if data_layer_mode not in _ALLOWED_OSM_DATA_LAYER_MODES:
        data_layer_mode = "auto"

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
        "data_layer_mode": data_layer_mode,
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
async def generate_chart(
    request: GenerateChartRequest,
    principal: Principal = Depends(get_principal),
):
    # Identity comes from the verified Principal; request.user_id is legacy input.
    user_id = principal.user_id
    await _verify_query_owner(
        query_id=request.query_id, user_id=user_id, connection=request.connection
    )
    request_started_at = (
        await _chart_request_watermark() if request.query_id else None
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
        parsed = extract_json_object(raw, reject_non_finite=True)
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

        await _persist_chart_baseline(
            query_id=request.query_id,
            user_id=user_id,
            chart_spec=spec,
            chart_config=chart_config,
            request_started_at=request_started_at,
        )
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
_MAP_EDIT_SPEC_FIELDS = {
    "location", "location_parts", "latitude", "longitude", "value", "value2",
    "aggregate", "value_format", "currency_symbol", "show_unmatched", "title",
    "y_label", "map_palette", "data_layer_mode",
}
_STANDARD_EDIT_SPEC_FIELDS = {
    "chart_type", "x", "y", "series", "stacked", "stack", "sort",
}
_STANDARD_EDIT_CHART_TYPES = {
    "bar", "line", "area", "pie", "donut", "scatter", "horizontal_bar",
    "stacked_bar", "stacked_area", "combo", "heatmap", "gauge",
}
_STANDARD_EDIT_CONTRACT_APPENDIX = """

ADDITIONAL SAFE EDIT CONTRACT:
- For a semantic change to chart type, x/y bindings, series grouping, stacking,
  or sort order, include a top-level "spec_patch" object containing only:
  chart_type, x, y, series, stacked, sort.
- Names in x, y, and series must be exact names from COLUMN NAMES.
- Do not directly rewrite data arrays for semantic changes. The server rebuilds
  them from the cached full result set.
- For view/style-only changes, omit spec_patch and preserve every category and
  value. Never emit dataset, transform, graphic, map/geo, HTML, URLs, images,
  JavaScript formatters, NaN, or Infinity.
"""
_LEGACY_EDIT_CONTRACT_APPENDIX = """

LEGACY CONTRACT VERSION 1 (mandatory for this request):
Return JSON with chart_config (the complete edited option), chart_type,
optional spec_patch, optional jeenFormat, derived_series, notes, and
out_of_scope. Preserve all existing data/category arrays exactly. Never emit
functions, HTML, URLs, graphic, dataset, transform, geo, map, NaN, or Infinity.
Use spec_patch rather than rewriting arrays for chart type, x/y/series binding,
stack, or sort changes. Derived overlays must use only the allowlisted operator
names and columns supplied below. Re-grouping or a new aggregation is out of
scope. Return JSON only.
"""


def _is_osm_map_edit(request: EditChartRequest) -> bool:
    spec = request.chart_spec if isinstance(request.chart_spec, dict) else {}
    return (
        spec.get("chart_type") == "osm_map"
        or isinstance(request.current_config.get("jeenOsmMap"), dict)
    )


def _dataset_from_edit_request(request: EditChartRequest) -> Optional[dict]:
    if request.all_data and request.column_names:
        return {"columns": list(request.column_names), "rows": request.all_data}
    return None


def _active_derived_series(request: EditChartRequest) -> list[DerivedSeriesSpec]:
    return list(request.active_derived_series or [])


def _edit_rejection(
    request: EditChartRequest,
    *,
    code: str,
    message: str,
    details: Optional[dict[str, Any]] = None,
    prompt: Optional[str] = None,
) -> EditChartResponse:
    return EditChartResponse(
        chart_config=request.current_config,
        chart_type=extract_chart_type(request.current_config),
        chart_spec=request.chart_spec if isinstance(request.chart_spec, dict) else None,
        derived_series=_active_derived_series(request),
        notes=message,
        out_of_scope=True,
        error=ChartEditError(code=code, message=message, details=details or {}),
        prompt=prompt,
    )


def _canonical_column(value: Any, columns: list[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    lowered = {column.casefold(): column for column in columns}
    return lowered.get(value.strip().casefold())


def _semantic_type_from_config(config: dict) -> Optional[str]:
    series = config.get("series")
    if not isinstance(series, list) or not series:
        return None
    items = [item for item in series if isinstance(item, dict)]
    if (
        isinstance(config.get("jeenMap"), dict)
        or any(item.get("coordinateSystem") == "geo" for item in items)
        or any(str(item.get("type") or "").lower() == "map" for item in items)
    ):
        return "map"
    types = {str(item.get("type") or "").lower() for item in items}
    if not types:
        return None
    if types == {"pie"}:
        radius = items[0].get("radius")
        return "donut" if isinstance(radius, list) and len(radius) >= 2 else "pie"
    if len(types) > 1:
        return "combo"
    series_type = next(iter(types))
    if series_type == "bar":
        _, y_categories = _category_axis_for_route(config, "yAxis")
        if y_categories:
            return "horizontal_bar"
        return "stacked_bar" if any(item.get("stack") for item in items) else "bar"
    if series_type == "line":
        if any(item.get("stack") for item in items):
            return "stacked_area"
        return "area" if any(isinstance(item.get("areaStyle"), dict) for item in items) else "line"
    return series_type if series_type in _STANDARD_EDIT_CHART_TYPES else None


def _category_axis_for_route(config: dict, key: str) -> tuple[Optional[dict], list[Any]]:
    axis = config.get(key)
    if isinstance(axis, list):
        axis = next((item for item in axis if isinstance(item, dict)), None)
    if not isinstance(axis, dict):
        return None, []
    data = axis.get("data")
    return axis, data if isinstance(data, list) else []


def _validate_standard_spec_patch(
    raw_patch: Any,
    *,
    column_names: list[str],
    numeric_cols: list[str],
) -> tuple[Optional[dict[str, Any]], Optional[tuple[str, str, dict[str, Any]]]]:
    if not isinstance(raw_patch, dict) or not raw_patch:
        return None, None
    unknown = sorted(set(raw_patch) - _STANDARD_EDIT_SPEC_FIELDS)
    if unknown:
        return None, (
            "invalid_spec_patch",
            "The semantic chart edit contained unsupported fields.",
            {"fields": unknown},
        )

    patch: dict[str, Any] = {}
    if "chart_type" in raw_patch:
        chart_type = str(raw_patch.get("chart_type") or "").strip().lower()
        if chart_type not in _STANDARD_EDIT_CHART_TYPES:
            return None, (
                "unsupported_chart_type",
                "That chart type is not supported for a deterministic chat edit.",
                {"chart_type": chart_type},
            )
        patch["chart_type"] = chart_type

    if "x" in raw_patch:
        x = _canonical_column(raw_patch.get("x"), column_names)
        if not x:
            return None, (
                "unknown_column",
                "The requested X-axis column is not in this result set.",
                {"column": raw_patch.get("x")},
            )
        patch["x"] = x

    if "y" in raw_patch:
        raw_y = raw_patch.get("y")
        raw_y = [raw_y] if isinstance(raw_y, str) else raw_y
        if not isinstance(raw_y, list) or not raw_y:
            return None, ("invalid_y_binding", "A Y-axis edit needs at least one measure.", {})
        y = [_canonical_column(value, column_names) for value in raw_y]
        if any(value is None for value in y) or any(value not in numeric_cols for value in y):
            return None, (
                "unknown_measure",
                "Every requested Y-axis field must be a numeric column in this result set.",
                {"columns": raw_y},
            )
        patch["y"] = list(dict.fromkeys(y))

    if "series" in raw_patch:
        if raw_patch.get("series") is None:
            patch["series"] = None
        else:
            series = _canonical_column(raw_patch.get("series"), column_names)
            if not series:
                return None, (
                    "unknown_column",
                    "The requested series column is not in this result set.",
                    {"column": raw_patch.get("series")},
                )
            patch["series"] = series

    stack_value = raw_patch.get("stacked", raw_patch.get("stack"))
    if "stacked" in raw_patch or "stack" in raw_patch:
        if not isinstance(stack_value, bool):
            return None, ("invalid_stack", "The stacked field must be true or false.", {})
        patch["stacked"] = stack_value

    if "sort" in raw_patch:
        sort = str(raw_patch.get("sort") or "").strip().lower()
        if sort not in _ALLOWED_SORTS:
            return None, (
                "invalid_sort",
                "Sort must be asc, desc, or none.",
                {"sort": sort},
            )
        patch["sort"] = sort
    return patch, None


def _merge_active_derived(
    request: EditChartRequest, parsed_items: Any
) -> list[DerivedSeriesSpec]:
    existing = [item.model_dump(mode="json") for item in _active_derived_series(request)]
    generated = normalise_derived_series(parsed_items, request.column_names)
    merged: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in [*existing, *generated]:
        key = (item.get("operator"), item.get("source_column"), item.get("label"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
        if len(merged) >= 4:
            break
    return [DerivedSeriesSpec(**item) for item in merged]


def _validate_map_view_commands(commands: Any) -> list[dict[str, Any]]:
    """Allow only deterministic browser commands over server-approved layers."""
    manifest = browser_map_layers()
    basemaps = {layer["id"] for layer in manifest.get("basemaps", [])}
    overlays = {
        layer["id"]
        for section in ("overlays", "vectorOverlays")
        for layer in manifest.get(section, [])
    }
    accepted: list[dict[str, Any]] = []
    for command in commands if isinstance(commands, list) else []:
        if not isinstance(command, dict):
            continue
        op = str(command.get("op") or "").strip()
        if op == "set_basemap" and command.get("layer_id") in basemaps:
            accepted.append({"op": op, "layer_id": command["layer_id"]})
        elif op == "set_overlays":
            layer_ids = command.get("layer_ids")
            if isinstance(layer_ids, list):
                accepted.append({
                    "op": op,
                    "layer_ids": [layer_id for layer_id in layer_ids if layer_id in overlays],
                })
        elif op == "set_user_data_visible" and isinstance(command.get("visible"), bool):
            accepted.append({"op": op, "visible": command["visible"]})
        elif op == "set_data_mode" and command.get("mode") in _ALLOWED_OSM_DATA_LAYER_MODES:
            accepted.append({"op": op, "mode": command["mode"]})
        elif op == "fit_extent":
            accepted.append({"op": op})
        elif op == "focus_place" and isinstance(command.get("query"), str):
            query = command["query"].strip()[:200]
            if query:
                accepted.append({"op": op, "query": query})
        elif op == "select_place" and isinstance(command.get("place_key"), str):
            place_key = command["place_key"].strip()[:256]
            if place_key:
                accepted.append({"op": op, "place_key": place_key})
        elif op == "clear_selection":
            accepted.append({"op": op})
        elif op == "toggle_sidebar" and isinstance(command.get("collapsed"), bool):
            accepted.append({"op": op, "collapsed": command["collapsed"]})
        elif op == "toggle_layers" and isinstance(command.get("open"), bool):
            accepted.append({"op": op, "open": command["open"]})
    return accepted


async def _edit_osm_map_chart(
    request: EditChartRequest,
    instruction: str,
    agent: Any,
    *,
    user_id: str,
) -> EditChartResponse:
    """Turn an LLM map edit into a validated spec rebuild plus safe view commands."""
    base_spec = request.chart_spec if isinstance(request.chart_spec, dict) else {}
    if base_spec.get("chart_type") != "osm_map":
        return EditChartResponse(
            chart_config=request.current_config,
            chart_type="osm_map",
            notes="This map needs its chart specification before it can be edited.",
            out_of_scope=True,
        )

    column_types_blob = (
        "\n".join(f"- {c.name} ({c.type})" for c in request.columns) or "(unknown)"
    )
    from src.api import state as app_state
    if app_state.prompt_cache:
        try:
            template = await app_state.prompt_cache.get_content("chart_map_editor")
            model_override = await app_state.prompt_cache.get_model_override("chart_map_editor")
        except Exception:
            template = _load_map_chart_editor_prompt()
            model_override = None
    else:
        template = _load_map_chart_editor_prompt()
        model_override = None
    system_prompt = template.format(
        instruction=instruction,
        chart_spec=json.dumps(base_spec, ensure_ascii=False),
        layer_manifest=json.dumps(browser_map_layers(), ensure_ascii=False),
        column_names=json.dumps(request.column_names, ensure_ascii=False),
        column_types=column_types_blob,
        recent_messages=_format_recent_messages(request.recent_messages),
    )
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
    except Exception as exc:  # noqa: BLE001
        logger.exception("Map chart edit LLM call failed")
        return EditChartResponse(
            chart_config=request.current_config,
            chart_type="osm_map",
            chart_spec=base_spec,
            notes=f"Sorry, the map-edit service is unavailable right now ({exc}).",
            out_of_scope=True,
            prompt=system_prompt,
        )

    parsed = extract_json_object(
        response.get("content") or "", reject_non_finite=True
    )
    if not isinstance(parsed, dict):
        return EditChartResponse(
            chart_config=request.current_config,
            chart_type="osm_map",
            chart_spec=base_spec,
            notes="I couldn't apply that map edit. Please rephrase it.",
            out_of_scope=True,
            prompt=system_prompt,
        )
    notes = parsed.get("notes")
    notes = notes.strip()[:300] if isinstance(notes, str) and notes.strip() else None
    if bool(parsed.get("out_of_scope")):
        return EditChartResponse(
            chart_config=request.current_config,
            chart_type="osm_map",
            chart_spec=base_spec,
            notes=notes,
            out_of_scope=True,
            prompt=system_prompt,
        )

    raw_patch = parsed.get("spec_patch")
    patch = {
        key: value for key, value in raw_patch.items()
        if key in _MAP_EDIT_SPEC_FIELDS
    } if isinstance(raw_patch, dict) else {}
    view_commands = _validate_map_view_commands(parsed.get("view_commands"))
    if not patch:
        return EditChartResponse(
            chart_config=request.current_config,
            chart_type="osm_map",
            chart_spec=base_spec,
            view_commands=view_commands,
            notes=notes,
            out_of_scope=False,
            rebuild_required=False,
            prompt=system_prompt,
        )

    dataset = result_cache.get(
        user_id=user_id, connection=request.connection, query_id=request.query_id,
    ) or _dataset_from_edit_request(request)
    if dataset is None:
        raise HTTPException(status_code=409, detail="cache_miss")
    profile = profile_dataset(dataset)
    column_names, numeric_cols, date_cols = _columns_from_profile(profile)
    geo_hints = await _load_geo_hints(request.connection, column_names)
    geo_roles = infer_geo_roles(dataset, geo_hints)
    merged = {**base_spec, **patch, "chart_type": "osm_map"}
    if isinstance(patch.get("location_parts"), dict):
        merged["location_parts"] = patch["location_parts"]
    spec = _validate_chart_spec(
        merged,
        column_names=column_names,
        numeric_cols=numeric_cols,
        date_cols=date_cols,
        forced_type="osm_map",
        value_col=patch.get("value") if "value" in patch else None,
        value2_col=(
            "" if "value2" in patch and patch["value2"] is None
            else (patch.get("value2") if "value2" in patch else None)
        ),
        aggregate_override=patch.get("aggregate") if "aggregate" in patch else None,
        geo_hints=geo_hints,
        geo_roles=geo_roles,
        osm_enabled=osm_maps_enabled(),
    )
    if spec["chart_type"] != "osm_map":
        return EditChartResponse(
            chart_config=request.current_config,
            chart_type="osm_map",
            chart_spec=base_spec,
            notes="Those bindings do not produce a valid point map.",
            out_of_scope=True,
            prompt=system_prompt,
        )
    spec["resolved_locations"] = await resolve_osm_locations(spec, dataset)
    chart_config = build_chart_option(spec, dataset)
    return EditChartResponse(
        chart_config=chart_config,
        chart_type="osm_map",
        chart_spec=spec,
        view_commands=view_commands,
        notes=notes,
        out_of_scope=False,
        rebuild_required=True,
        prompt=system_prompt,
    )


@router.post(
    "/edit-chart/rebuild",
    response_model=EditChartRebuildResponse,
)
async def rebuild_chart_bindings(
    request: EditChartRebuildRequest,
    response: Response,
    principal: Principal = Depends(get_principal),
) -> EditChartRebuildResponse:
    """Rebuild a SQL chart from cached rows after validated binding or chart-type changes."""

    total_started = time.monotonic()
    phases = {"cache": 0.0, "profile": 0.0, "build": 0.0, "validate": 0.0}
    cache_status = "not_read"

    def finish(status: str, error_code: Optional[str] = None) -> dict[str, str]:
        total_ms = round((time.monotonic() - total_started) * 1000, 2)
        timing = ", ".join(
            [
                f"cache;dur={phases['cache']}",
                f"profile;dur={phases['profile']}",
                f"build;dur={phases['build']}",
                f"validate;dur={phases['validate']}",
                f"total;dur={total_ms}",
            ]
        )
        response.headers["Server-Timing"] = timing
        logger.info(
            "chart_edit_rebuild_timing query_id=%s cache=%s status=%s "
            "cache_ms=%s profile_ms=%s build_ms=%s validate_ms=%s "
            "total_ms=%s error_code=%s",
            request.query_id,
            cache_status,
            status,
            phases["cache"],
            phases["profile"],
            phases["build"],
            phases["validate"],
            total_ms,
            error_code,
            extra={
                "event": "chart_edit_rebuild_timing",
                "query_id": request.query_id,
                "cache_status": cache_status,
                "status": status,
                "cache_ms": phases["cache"],
                "profile_ms": phases["profile"],
                "build_ms": phases["build"],
                "validate_ms": phases["validate"],
                "total_ms": total_ms,
                "error_code": error_code,
            },
        )
        return {"Server-Timing": timing}

    # Ownership is deliberately checked before even looking in the process cache.
    user_id = principal.user_id
    await _verify_query_owner(
        query_id=request.query_id,
        user_id=user_id,
        connection=request.connection,
    )

    base_type = str(request.chart_spec.get("chart_type") or "").strip().lower()
    if base_type not in _STANDARD_EDIT_CHART_TYPES:
        headers = finish("rejected", "unsupported_chart_kind")
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unsupported_chart_kind",
                "message": "Only SQL chart specifications can be rebound.",
            },
            headers=headers,
        )

    cache_started = time.monotonic()
    dataset = result_cache.get(
        user_id=user_id,
        connection=request.connection,
        query_id=request.query_id,
    )
    if dataset is not None:
        cache_status = "hit"
    elif request.column_names is not None and request.all_data is not None:
        dataset = {
            "columns": list(request.column_names),
            "rows": request.all_data,
        }
        cache_status = "fallback"
    else:
        cache_status = "miss"
    phases["cache"] = round((time.monotonic() - cache_started) * 1000, 2)

    if dataset is None:
        headers = finish("cache_miss", "cache_miss")
        raise HTTPException(status_code=409, detail="cache_miss", headers=headers)

    profile_started = time.monotonic()
    profile = profile_dataset(dataset)
    column_names, numeric_cols, date_cols = _columns_from_profile(profile)
    phases["profile"] = round((time.monotonic() - profile_started) * 1000, 2)
    if not column_names:
        headers = finish("rejected", "missing_columns")
        raise HTTPException(
            status_code=422,
            detail={
                "code": "missing_columns",
                "message": "Could not determine chartable columns for this result set.",
            },
            headers=headers,
        )

    validate_started = time.monotonic()
    raw_patch: dict[str, Any] = {}
    for operation in request.operations:
        raw_patch.update(
            operation.model_dump(
                mode="json",
                exclude={"op"},
                exclude_unset=True,
            )
        )
    patch, patch_error = _validate_standard_spec_patch(
        raw_patch,
        column_names=column_names,
        numeric_cols=numeric_cols,
    )
    if patch_error:
        phases["validate"] += round(
            (time.monotonic() - validate_started) * 1000, 2
        )
        code, message, details = patch_error
        headers = finish("rejected", code)
        raise HTTPException(
            status_code=422,
            detail={"code": code, "message": message, "details": details},
            headers=headers,
        )

    merged = {**request.chart_spec, **(patch or {})}
    # A validated type change wins; otherwise keep the chart's current type so
    # a binding-only edit never silently re-picks a chart kind.
    target_type = str((patch or {}).get("chart_type") or base_type)
    spec = _validate_chart_spec(
        merged,
        column_names=column_names,
        numeric_cols=numeric_cols,
        date_cols=date_cols,
        forced_type=target_type,
        osm_enabled=False,
    )
    phases["validate"] += round(
        (time.monotonic() - validate_started) * 1000, 2
    )

    build_started = time.monotonic()
    try:
        chart_config = build_chart_option(spec, dataset)
    except (TypeError, ValueError) as exc:
        phases["build"] = round((time.monotonic() - build_started) * 1000, 2)
        headers = finish("rejected", "chart_rebuild_failed")
        raise HTTPException(
            status_code=422,
            detail={
                "code": "chart_rebuild_failed",
                "message": "The chart could not be rebuilt from these bindings.",
                "details": {"reason": str(exc)},
            },
            headers=headers,
        ) from exc
    phases["build"] = round((time.monotonic() - build_started) * 1000, 2)

    validate_started = time.monotonic()
    validation = validate_chart_edit(chart_config, chart_config)
    phases["validate"] += round(
        (time.monotonic() - validate_started) * 1000, 2
    )
    if not validation.ok:
        code = validation.code or "invalid_rebuild"
        headers = finish("rejected", code)
        raise HTTPException(
            status_code=422,
            detail={
                "code": code,
                "message": validation.message
                or "The rebuilt chart was not safe to apply.",
                "details": validation.details,
            },
            headers=headers,
        )

    finish("success")
    return EditChartRebuildResponse(
        chart_config=chart_config,
        chart_spec=spec,
    )


def _v2_rejection(code: str, message: str) -> EditChartV2Response:
    return EditChartV2Response(
        operations=[],
        notes=message,
        out_of_scope=True,
        reason_code=code,
    )


async def _edit_chart_v2(
    request: EditChartRequest,
    instruction: str,
    http_response: Response,
) -> EditChartV2Response:
    """Run the isolated, data-free chart operation contract with one LLM call."""

    from src.api import state as app_state

    total_started = time.monotonic()
    phases = {"context": 0, "prompt": 0, "model": 0, "validate": 0}
    finish_reason: Optional[str] = None
    usage: dict[str, Any] = {}

    context_started = time.monotonic()
    manifest = request.chart_manifest
    manifest_blob = json.dumps(
        manifest.model_dump(by_alias=True, mode="json", exclude_none=True)
        if manifest is not None
        else {},
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    recent_blob = _format_recent_messages(request.recent_messages)
    phases["context"] = round((time.monotonic() - context_started) * 1000, 2)

    result: EditChartV2Response
    prompt_started = time.monotonic()
    template = _load_chart_editor_prompt()
    model_override = None
    if app_state.prompt_cache:
        try:
            template = await app_state.prompt_cache.get_content("chart_editor")
            model_override = await app_state.prompt_cache.get_model_override("chart_editor")
        except Exception:  # noqa: BLE001
            logger.warning("Chart operation prompt cache unavailable; using file prompt")
    if not isinstance(template, str) or not template.strip():
        template = _load_chart_editor_prompt()
    system_prompt = (
        template.strip()
        + "\n\n"
        + CHART_OPERATION_SAFETY_CONTRACT.strip()
        + "\n\nREQUEST CONTEXT (data-free)\n"
        + f"chart_kind: {request.chart_kind}\n"
        + f"chart_manifest: {manifest_blob}\n"
        + f"recent_messages:\n{recent_blob}"
    )
    phases["prompt"] = round((time.monotonic() - prompt_started) * 1000, 2)

    llm = app_state.llm_service
    if llm is None:
        result = _v2_rejection(
            "edit_service_unavailable",
            "The chart-edit service is unavailable right now.",
        )
    else:
        model_started = time.monotonic()
        try:
            # This is the sole model call in the v2 path. It deliberately bypasses
            # connection agents, query caches, connector runners, and analysis.
            model_response = await llm.generate(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": instruction},
                ],
                temperature=EDIT_CHART_OPERATIONS_PARAMS.temperature,
                max_tokens=EDIT_CHART_OPERATIONS_PARAMS.max_tokens,
                model_override=model_override,
                # A chat box must fail fast: one bounded attempt, no provider
                # fallback chain that could double the wait.
                timeout=EDIT_CHART_LLM_TIMEOUT_SECONDS,
                max_fallbacks=0,
            )
            finish_reason = model_response.get("finish_reason")
            raw_usage = model_response.get("usage")
            usage = raw_usage if isinstance(raw_usage, dict) else {}
        except Exception:  # noqa: BLE001
            logger.exception("Chart operation LLM call failed")
            model_response = None
        phases["model"] = round((time.monotonic() - model_started) * 1000, 2)

        if model_response is None:
            result = _v2_rejection(
                "edit_service_unavailable",
                "The chart-edit service is unavailable right now.",
            )
        else:
            validate_started = time.monotonic()
            raw = model_response.get("content") or ""
            truncated = str(finish_reason or "").lower() in {
                "length",
                "max_tokens",
                "max_completion_tokens",
            }
            parsed = None if truncated else extract_json_object(raw, reject_non_finite=True)
            if truncated:
                result = _v2_rejection(
                    "model_output_truncated",
                    "The chart edit response was incomplete. Please try a shorter instruction.",
                )
            elif not isinstance(parsed, dict):
                logger.warning(
                    "Chart operation LLM returned unparseable JSON (%d chars)", len(raw)
                )
                result = _v2_rejection(
                    "invalid_model_output",
                    "I couldn't safely apply that chart edit. Please rephrase it.",
                )
            else:
                try:
                    envelope = OPERATION_ENVELOPE_ADAPTER.validate_python(parsed)
                    envelope = validate_operation_envelope(
                        envelope,
                        chart_kind=request.chart_kind or "sql",
                        manifest=manifest,
                    )
                    result = EditChartV2Response(**envelope.model_dump(mode="json"))
                except OperationContractError as exc:
                    result = _v2_rejection(exc.code, exc.message)
                except ValidationError:
                    logger.warning("Chart operation LLM response failed schema validation")
                    result = _v2_rejection(
                        "invalid_model_output",
                        "I couldn't safely apply that chart edit. Please rephrase it.",
                    )
            phases["validate"] = round((time.monotonic() - validate_started) * 1000, 2)

    total_ms = round((time.monotonic() - total_started) * 1000, 2)
    http_response.headers["Server-Timing"] = ", ".join(
        [
            f"context;dur={phases['context']}",
            f"prompt;dur={phases['prompt']}",
            f"model;dur={phases['model']}",
            f"validate;dur={phases['validate']}",
            f"total;dur={total_ms}",
        ]
    )
    logger.info(
        "chart_edit_v2_timing chart_kind=%s context_ms=%s prompt_ms=%s "
        "model_ms=%s validate_ms=%s total_ms=%s operations=%d "
        "out_of_scope=%s reason_code=%s finish_reason=%s usage=%s",
        request.chart_kind,
        phases["context"],
        phases["prompt"],
        phases["model"],
        phases["validate"],
        total_ms,
        len(result.operations),
        result.out_of_scope,
        result.reason_code,
        finish_reason,
        usage,
        extra={
            "event": "chart_edit_v2_timing",
            "chart_kind": request.chart_kind,
            "context_ms": phases["context"],
            "prompt_ms": phases["prompt"],
            "model_ms": phases["model"],
            "validate_ms": phases["validate"],
            "total_ms": total_ms,
            "operation_count": len(result.operations),
            "out_of_scope": result.out_of_scope,
            "reason_code": result.reason_code,
            "finish_reason": finish_reason,
            "usage": usage,
        },
    )
    return result


@router.post(
    "/edit-chart",
    response_model=Union[EditChartV2Response, EditChartResponse],
)
async def edit_chart(
    request: EditChartRequest,
    response: Response,
    principal: Principal = Depends(get_principal),
) -> EditChartV2Response | EditChartResponse:
    """Apply a natural-language edit to the current ECharts config.

    The endpoint never touches the SQL result set. It returns a new chart
    config (potentially identical to the input on out-of-scope requests)
    plus an optional list of `derived_series` specs that the client computes
    from the existing dataset.
    """
    instruction = (request.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="`instruction` is required")
    if request.contract_version == 2:
        return await _edit_chart_v2(request, instruction, response)
    if not request.current_config:
        raise HTTPException(status_code=400, detail="`current_config` is required")

    user_id = principal.user_id
    await _verify_query_owner(
        query_id=request.query_id, user_id=user_id, connection=request.connection
    )
    agent = await resolve_agent(request.connection)
    instruction = instruction[:_CHART_EDITOR_MAX_INSTRUCTION_CHARS]
    # Natural-language edits are intentionally session-only. Generated,
    # selector-rebound and deterministic analysis charts establish the durable
    # baseline; chart chat never mutates it.
    return await _edit_chart_impl(request, instruction, agent, user_id=user_id)


async def _edit_chart_impl(
    request: EditChartRequest,
    instruction: str,
    agent: Any,
    *,
    user_id: str,
) -> EditChartResponse:
    if _is_osm_map_edit(request):
        return await _edit_osm_map_chart(request, instruction, agent, user_id=user_id)

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
        # Admin-customized prompts remain valid even when they predate
        # spec_patch: no new format placeholders are required.
        system_prompt += _STANDARD_EDIT_CONTRACT_APPENDIX
        system_prompt += _LEGACY_EDIT_CONTRACT_APPENDIX
        system_prompt += (
            "\n\nLEGACY REQUEST CONTEXT\n"
            f"instruction: {instruction}\n"
            f"column_names: {column_names_blob}\n"
            f"column_types:\n{column_types_blob}\n"
            f"sample_rows: {sample_blob}\n"
            f"current_config: {config_blob}\n"
            f"recent_messages:\n{recent_blob}\n"
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
        return _edit_rejection(
            request,
            code="edit_service_unavailable",
            message=f"Sorry, the chart-edit service is unavailable right now ({e}).",
            prompt=system_prompt,
        )

    raw = response.get("content") or ""
    parsed = extract_json_object(raw, reject_non_finite=True)
    if not isinstance(parsed, dict):
        logger.warning("Chart-edit LLM returned unparseable JSON (%d chars)", len(raw))
        return _edit_rejection(
            request,
            code="invalid_model_json",
            message="I couldn't apply that edit. Please rephrase, or try one of the suggestions.",
            prompt=system_prompt,
        )

    notes = parsed.get("notes")
    notes = notes.strip()[:300] if isinstance(notes, str) and notes.strip() else None
    if bool(parsed.get("out_of_scope")):
        return _edit_rejection(
            request,
            code="out_of_scope",
            message=notes or "That change needs a new chart or query.",
            prompt=system_prompt,
        )

    raw_patch = parsed.get("spec_patch")
    if isinstance(raw_patch, dict) and raw_patch:
        base_spec = request.chart_spec if isinstance(request.chart_spec, dict) else None
        if not base_spec:
            return _edit_rejection(
                request,
                code="semantic_rebuild_unavailable",
                message="That semantic edit needs the chart specification. Regenerate the chart and try again.",
                prompt=system_prompt,
            )
        dataset = result_cache.get(
            user_id=user_id,
            connection=request.connection,
            query_id=request.query_id,
        ) or _dataset_from_edit_request(request)
        if dataset is None:
            # Match /generate-chart and OSM edit semantics: the browser retries
            # once with the full rows when this replica no longer has the result.
            raise HTTPException(status_code=409, detail="cache_miss")
        profile = profile_dataset(dataset)
        column_names, numeric_cols, date_cols = _columns_from_profile(profile)
        patch, patch_error = _validate_standard_spec_patch(
            raw_patch,
            column_names=column_names,
            numeric_cols=numeric_cols,
        )
        if patch_error:
            code, message, details = patch_error
            return _edit_rejection(
                request,
                code=code,
                message=message,
                details=details,
                prompt=system_prompt,
            )
        merged = {**base_spec, **(patch or {})}
        if "stacked" in (patch or {}):
            stacked = bool(patch["stacked"])
            chart_type = str(merged.get("chart_type") or "bar").lower()
            if stacked and chart_type in {"bar", "stacked_bar"}:
                merged["chart_type"] = "stacked_bar"
            elif stacked and chart_type in {"line", "area", "stacked_area"}:
                merged["chart_type"] = "stacked_area"
            elif not stacked and chart_type == "stacked_bar":
                merged["chart_type"] = "bar"
            elif not stacked and chart_type == "stacked_area":
                merged["chart_type"] = "area"
        spec = _validate_chart_spec(
            merged,
            column_names=column_names,
            numeric_cols=numeric_cols,
            date_cols=date_cols,
            osm_enabled=False,
        )
        try:
            chart_config = build_chart_option(spec, dataset)
        except (TypeError, ValueError) as exc:
            return _edit_rejection(
                request,
                code="semantic_rebuild_failed",
                message="That semantic edit could not be rebuilt from the result set.",
                details={"reason": str(exc)},
                prompt=system_prompt,
            )
        safe_rebuild = validate_chart_edit(chart_config, chart_config)
        if not safe_rebuild.ok:
            return _edit_rejection(
                request,
                code=safe_rebuild.code or "invalid_rebuild",
                message=safe_rebuild.message or "The rebuilt chart was not safe to apply.",
                details=safe_rebuild.details,
                prompt=system_prompt,
            )
        return EditChartResponse(
            chart_config=chart_config,
            chart_type=spec["chart_type"],
            chart_spec=spec,
            derived_series=_merge_active_derived(request, parsed.get("derived_series")),
            notes=notes,
            out_of_scope=False,
            rebuild_required=True,
            prompt=system_prompt,
        )

    chart_config = parsed.get("chart_config")
    if not isinstance(chart_config, dict):
        return _edit_rejection(
            request,
            code="missing_chart_config",
            message="The chart editor did not return a chart configuration.",
            prompt=system_prompt,
        )

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

    validation = validate_chart_edit(request.current_config, chart_config)
    if not validation.ok:
        return _edit_rejection(
            request,
            code=validation.code or "invalid_chart_config",
            message=validation.message or "The proposed chart edit was rejected.",
            details=validation.details,
            prompt=system_prompt,
        )

    current_semantic_type = (
        str(request.chart_spec.get("chart_type") or "").lower()
        if isinstance(request.chart_spec, dict)
        else _semantic_type_from_config(request.current_config)
    )
    candidate_semantic_type = _semantic_type_from_config(chart_config)
    if (
        current_semantic_type
        and candidate_semantic_type
        and current_semantic_type != candidate_semantic_type
    ):
        return _edit_rejection(
            request,
            code="semantic_patch_required",
            message="Chart-type changes require a deterministic spec rebuild.",
            details={
                "current_chart_type": current_semantic_type,
                "requested_chart_type": candidate_semantic_type,
            },
            prompt=system_prompt,
        )

    return EditChartResponse(
        chart_config=chart_config,
        chart_type=candidate_semantic_type or extract_chart_type(chart_config),
        chart_spec=request.chart_spec if isinstance(request.chart_spec, dict) else None,
        derived_series=_merge_active_derived(request, parsed.get("derived_series")),
        notes=notes,
        out_of_scope=False,
        prompt=system_prompt,
    )


# ----------------------------------------------------------------------
# One-shot enhancement of an existing chart config
# ----------------------------------------------------------------------
@router.post("/enhance-chart")
async def enhance_chart_endpoint(
    request: EnhanceChartRequest,
    _principal: Principal = Depends(get_principal),
):
    # The middleware already enforces the internal token; the explicit
    # dependency keeps the identity requirement visible like the other routes.
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
        enhanced_config = extract_json_object(raw, reject_non_finite=True)
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
