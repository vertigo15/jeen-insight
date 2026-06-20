"""Chart endpoints: initial generation, enhancement, and chat-driven edits."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException

from src.api.chart_builder import build_chart_option, profile_dataset
from src.api.dependencies import resolve_agent
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

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["charts"])


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
    "stacked_bar", "stacked_area", "combo", "heatmap", "gauge",
}
_ALLOWED_AGGREGATES = {"sum", "avg", "count", "min", "max", "none"}
_ALLOWED_SORTS = {"asc", "desc", "none"}
_ALLOWED_FORMATS = {"number", "currency", "percent", "none"}


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
    '  "chart_type": "bar|line|area|pie|donut|scatter|horizontal_bar|stacked_bar|stacked_area|combo|heatmap|gauge",\n'
    '  "x": "<column for the category or time axis (pie/donut label dimension)>",\n'
    '  "x_parts": ["<col>", "<col>"]  // OPTIONAL: 2+ columns to join into one ordered axis label, e.g. ["year","month"]. Omit or null otherwise.,\n'
    '  "y": ["<one or more numeric measure columns>"],\n'
    '  "series": "<column to split into multiple series/segments, or null>",\n'
    '  "aggregate": "sum|avg|count|min|max|none",\n'
    '  "sort": "asc|desc|none",\n'
    '  "top_n": <integer or null>,\n'
    '  "title": "<concise human title>",\n'
    '  "x_label": "<axis label or null>",\n'
    '  "y_label": "<axis label or null>",\n'
    '  "value_format": "number|currency|percent|none",\n'
    '  "currency_symbol": "<currency symbol like $, €, £, ₪ — ONLY if the currency is known; else null>",\n'
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
    "- Use combo ONLY for two measures with different units/scales (e.g. revenue as\n"
    "  bars + margin %% as line); otherwise prefer a single type.\n"
    "- Don't put high-cardinality IDs/keys (order id, customer id) on x — aggregate\n"
    "  to a meaningful category or time instead.\n"
    "- For ranking questions (top/bottom/most/least) use horizontal_bar + sort=desc.\n\n"
    "RULES:\n"
    "- x, x_parts[], y[], and series MUST be exact column names from the schema.\n"
    "- y must be numeric measures; aggregate when x (and series) repeats. Prefer\n"
    "  sum for additive quantities and avg for rates/ratios/prices.\n"
    "- Sort categorical charts by the measure desc unless x is time (chronological).\n"
    "- Use the sample rows ONLY to understand shape/meaning, never to copy values."
)


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
) -> dict:
    """Clamp the LLM spec to safe, real values and apply user overrides.

    Guarantees: valid chart_type, an x dimension, ≥1 numeric measure, and
    enumerations limited to the allowed sets. Falls back to sensible defaults
    derived from the detected column types when the LLM is vague or wrong.
    """
    lowered = {c.lower(): c for c in column_names}
    numeric_set = set(numeric_cols)
    non_numeric = [c for c in column_names if c not in numeric_set]

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

    # y measures
    y = [c for c in _coerce_columns(spec.get("y"), lowered) if c in numeric_set]
    if not y:
        y = numeric_cols[:1] or ([c for c in column_names if c != x][:1])

    # series (group-by) — must differ from x
    series_list = _coerce_columns(spec.get("series"), lowered)
    series = next((c for c in series_list if c != x), None)

    # Composite x-axis: e.g. separate year + month columns joined into one
    # ordered time label. Only honoured when ≥2 real columns are named.
    x_parts = _coerce_columns(spec.get("x_parts"), lowered)
    x_parts = x_parts if len(x_parts) >= 2 else None

    aggregate = str(spec.get("aggregate", "")).strip().lower()
    if aggregate not in _ALLOWED_AGGREGATES:
        aggregate = "sum"

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
    if forced_type and forced_type not in ("auto", "", None):
        forced = forced_type.strip().lower()
        if forced in _ALLOWED_CHART_TYPES:
            chart_type = forced
    if x_col and x_col in column_names:
        x = x_col
        x_parts = None  # explicit single-column choice overrides a composite axis
    if y_col and y_col in column_names:
        y = [y_col]
    if series_col is not None:
        series = series_col if series_col in column_names else None
    if series == x:
        series = None
    # A composite part must not double as the series split.
    if x_parts and series in x_parts:
        series = None

    return {
        "chart_type": chart_type,
        "x": x,
        "x_parts": x_parts,
        "y": y,
        "series": series,
        "aggregate": aggregate,
        "sort": sort,
        "top_n": top_n,
        "title": title.strip()[:120],
        "x_label": _label("x_label"),
        "y_label": _label("y_label"),
        "value_format": value_format,
        "currency_symbol": currency_symbol,
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
    agent = await resolve_agent(request.connection)
    chart_type_param = (request.chart_type or "auto").strip().lower()

    # 1. Resolve the dataset: cache first, then client-sent fallback.
    dataset = result_cache.get(
        user_id=request.user_id,
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
    if mapping:
        instruction_parts.append(
            "User-selected column mapping (MUST follow): " + "; ".join(mapping)
        )
    instruction_blob = ("\n\n" + "\n".join(instruction_parts)) if instruction_parts else ""

    sample = list((dataset.get("rows") or [])[:50])
    user_prompt = (
        "Choose the best chart for this dataset and return the JSON spec.\n\n"
        f"Dataset profile (computed over ALL {profile.get('row_count', 0)} rows):\n"
        f"{_profile_blob(profile)}{instruction_blob}\n\n"
        f"Sample rows (first {len(sample)} of {profile.get('row_count', 0)}, for "
        "SHAPE/MEANING ONLY — do not copy these values into the chart):\n"
        + json.dumps(sample, indent=2, default=str)
        + "\n\nReturn ONLY the JSON spec."
    )

    try:
        response = await agent.llm.generate(
            messages=[
                {"role": "system", "content": _GENERATE_SYSTEM_PROMPT},
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
        )
        if not spec["x"] or not spec["y"]:
            raise HTTPException(
                status_code=422,
                detail="Could not determine chartable columns for this result set.",
            )

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
            system_message=_GENERATE_SYSTEM_PROMPT,
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
