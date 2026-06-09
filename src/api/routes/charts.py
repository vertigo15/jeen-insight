"""Chart endpoints: initial generation, enhancement, and chat-driven edits."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException

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
# Chart-type hints for explicit user selections
# ----------------------------------------------------------------------
_CHART_TYPE_GUIDANCE: dict[str, str] = {
    "bar": "Use series type 'bar' with a category xAxis and value yAxis.",
    "line": "Use series type 'line'; set smooth: true for time series when appropriate.",
    "pie": "Use series type 'pie' with data as [{name, value}, ...] from category + metric.",
    "area": "Use series type 'line' with areaStyle filled under the line.",
    "scatter": "Use series type 'scatter' with numeric x and y (value axes or scatter data pairs).",
    "horizontal_bar": "Use series type 'bar' with category on yAxis and value on xAxis.",
    "stacked_bar": (
        "Use multiple bar series sharing stack: 'total'. Split by the series column "
        "when provided, otherwise use a single stack group."
    ),
    "stacked_area": (
        "Use multiple line series with stack: 'total' and areaStyle on each series."
    ),
    "donut": (
        "Use series type 'pie' with radius: ['40%', '70%'] for a donut hole."
    ),
    "combo": (
        "Combine bar and line series on shared categories. Use yAxisIndex: 1 for the "
        "line when scales differ; add a second value yAxis if needed."
    ),
    "heatmap": (
        "Use series type 'heatmap' with xAxis/yAxis categories and data as "
        "[xIndex, yIndex, value] tuples. Include visualMap."
    ),
    "gauge": (
        "Use series type 'gauge' for a single KPI (first row / aggregate). "
        "Show the primary metric with min/max when inferable."
    ),
}


def _column_mapping_blob(request: GenerateChartRequest) -> str:
    parts: list[str] = []
    if request.x_column:
        parts.append(f"- X-axis / categories: {request.x_column}")
    if request.y_column:
        parts.append(f"- Y-axis / primary metric: {request.y_column}")
    if request.series_column:
        parts.append(f"- Series / color breakdown: {request.series_column}")
    if not parts:
        return ""
    return "\n\nUser-selected column mapping (MUST follow):\n" + "\n".join(parts)


# ----------------------------------------------------------------------
# Initial chart generation
# ----------------------------------------------------------------------
@router.post("/generate-chart", response_model=GenerateChartResponse)
async def generate_chart(request: GenerateChartRequest):
    agent = await resolve_agent(request.connection)
    chart_type_param = request.chart_type or "auto"

    system_prompt = (
        "You are a data visualization expert specializing in Apache ECharts.\n\n"
        "Analyze the data and return ONLY a valid ECharts configuration as JSON.\n\n"
        "STRICT JSON REQUIREMENTS:\n"
        "- Return pure JSON only \u2014 no explanation, no markdown fences (no ```), "
        "no comments (// or /* */), no JavaScript code.\n"
        "- All keys and string values must be double-quoted.\n"
        "- DO NOT use JavaScript function expressions anywhere (no "
        "`function (value) { ... }`). For formatters, use ECharts template strings "
        "such as \"{value}\", \"{c}\", \"{b}: {c}\", or \"{value}M\". If you need K/M/B "
        "abbreviations, pre-scale the data and put the unit in the axis label or in the "
        "formatter template (e.g. \"{value}K\").\n"
        "- No trailing commas. No undefined / NaN / single quotes. Use null for missing values.\n\n"
        "DESIGN GUIDELINES:\n"
        "- Choose an appropriate chart type for the data.\n"
        "- Apply smart number formatting (K/M/B abbreviations) via template strings.\n"
        "- Add a meaningful title and clear axis labels.\n"
        "- Include polished tooltips using template strings.\n"
        "- Ensure title and legend never overlap.\n"
        "- Supported explicit types include: bar, line, pie, area, scatter, horizontal_bar, "
        "stacked_bar, stacked_area, donut, combo, heatmap, gauge."
    )
    if chart_type_param == "auto":
        chart_type_instruction = ""
    else:
        guidance = _CHART_TYPE_GUIDANCE.get(chart_type_param, "")
        chart_type_instruction = (
            f"\n\nThe user has explicitly selected: {chart_type_param.upper()} CHART. "
            "You MUST return that chart type."
        )
        if guidance:
            chart_type_instruction += f"\nImplementation hint: {guidance}"
    column_mapping = _column_mapping_blob(request)
    user_prompt = (
        f"Create a chart visualization for this data.{chart_type_instruction}{column_mapping}\n\n"
        f"Column Names:\n{json.dumps(request.column_names)}\n\n"
        "Column Information (with detected types):\n"
        + "\n".join(f"- {c.name} ({c.type})" for c in request.columns)
        + "\n\nData (first "
        + str(len(request.sample_data))
        + " rows):\n"
        + json.dumps(request.sample_data, indent=2)
        + "\n\nReturn ONLY the ECharts configuration JSON. No explanatory text."
    )

    try:
        response = await agent.llm.generate(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=GENERATE_CHART_PARAMS.temperature,
            max_tokens=GENERATE_CHART_PARAMS.max_tokens,
        )
        raw = response.get("content") or ""
        chart_config = extract_json_object(raw)
        if chart_config is None:
            logger.error(
                "Chart LLM response was not parseable JSON. First 500 chars: %s",
                raw[:500],
            )
            raise HTTPException(
                status_code=500,
                detail="LLM did not return valid JSON for the chart configuration. Try again.",
            )
        if not isinstance(chart_config, dict) or "series" not in chart_config:
            logger.error(
                "Chart config missing 'series' field. Keys: %s",
                list(chart_config.keys()) if isinstance(chart_config, dict) else type(chart_config),
            )
            raise HTTPException(status_code=500, detail="Chart config missing 'series' field")

        chart_type = "bar"
        series = chart_config.get("series") or []
        if series:
            chart_type = series[0].get("type", "bar")

        return GenerateChartResponse(
            chart_config=chart_config,
            chart_type=chart_type,
            prompt=user_prompt,
            system_message=system_prompt,
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
