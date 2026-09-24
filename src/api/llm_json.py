"""Pure helpers for parsing and sanitising LLM-produced JSON payloads.

These functions live outside the route modules so they can be unit-tested
without spinning up the FastAPI app or any DB pool.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _reject_non_finite_constant(value: str) -> None:
    raise ValueError(f"non-finite number: {value}")


# ----------------------------------------------------------------------
# Generic JSON extraction
# ----------------------------------------------------------------------
def extract_json_object(
    raw: str, *, reject_non_finite: bool = False
) -> Optional[Dict[str, Any]]:
    """Best-effort decoder for LLM-produced JSON payloads.

    Strategy:
    1. Strip markdown fences and outer whitespace.
    2. Trim to the outermost `{...}`.
    3. Try strict ``json.loads``.
    4. If that fails, sanitise common LLM artefacts (JS comments, trailing
       commas, formatter function bodies) and retry.
    Returns ``None`` if both attempts fail.
    """
    if not raw:
        return None
    text = raw.strip()
    # Strip ```json or ``` fences
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", text)
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()
    # Trim to outermost braces
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]

    parse_constant = _reject_non_finite_constant if reject_non_finite else None
    load_kwargs = {"parse_constant": parse_constant} if parse_constant else {}
    try:
        return json.loads(text, **load_kwargs)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Strict JSON parse failed (%s); attempting cleanup", e)

    cleaned = sanitize_llm_json(text)
    try:
        return json.loads(cleaned, **load_kwargs)
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("Lenient JSON parse failed too: %s", e)
        logger.debug("Cleaned LLM text was: %s", cleaned[:1500])
        return None


def sanitize_llm_json(text: str) -> str:
    """Strip JS comments, trailing commas, and JS function bodies."""
    # Drop // line comments
    text = re.sub(r"//[^\n]*", "", text)
    # Drop /* block comments */
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    # Replace JS function expressions used in formatter / etc. with the literal
    # ECharts placeholder string '{value}' so the JSON is still valid.
    text = re.sub(
        r"function\s*\([^)]*\)\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}",
        '"{value}"',
        text,
        flags=re.S,
    )
    # Drop trailing commas: ,] or ,}
    text = re.sub(r",(\s*[\]}])", r"\1", text)
    return text.strip()


# ----------------------------------------------------------------------
# Autocomplete: corrections
# ----------------------------------------------------------------------
# Accept any of these key spellings from the LLM and coerce to {wrong, right}.
_CORRECTION_WRONG_KEYS = ("wrong", "from", "old", "misspelled", "typo", "input")
_CORRECTION_RIGHT_KEYS = ("right", "to", "new", "correct", "correction", "fixed")


def normalise_corrections(items: Any) -> List[Dict[str, str]]:
    """Coerce a parsed `corrections` payload into a clean list of ``{wrong, right}``.

    Accepts strings, ``{wrong, right}``, ``{from, to}``, ``{old, new}``,
    ``{misspelled, correct}``, arrow-strings like ``"prooduct -> product"``,
    and dicts mapping wrong->right. Drops empty / self-equal / duplicate
    entries; caps at 4.
    """
    out: List[Dict[str, str]] = []
    if not isinstance(items, list):
        return out
    seen: set[str] = set()
    for c in items:
        wrong = right = ""
        if isinstance(c, dict):
            for k in _CORRECTION_WRONG_KEYS:
                if c.get(k):
                    wrong = str(c.get(k)).strip()
                    break
            for k in _CORRECTION_RIGHT_KEYS:
                if c.get(k):
                    right = str(c.get(k)).strip()
                    break
            # Single-pair dict like {"prooduct": "product"}.
            if not (wrong and right) and len(c) == 1:
                k, v = next(iter(c.items()))
                if isinstance(k, str) and isinstance(v, str):
                    wrong, right = k.strip(), v.strip()
        elif isinstance(c, str):
            text = c.strip()
            for sep in ("->", "→", " => ", " : ", " — ", " - "):
                if sep in text:
                    a, _, b = text.partition(sep)
                    wrong, right = a.strip(" `'\""), b.strip(" `'\"")
                    break
            if not (wrong and right):
                # A bare string is only a useful correction if we can't tell
                # what was wrong; skip it (the UI has no anchor to replace).
                continue
        if not wrong or not right:
            continue
        if wrong.lower() == right.lower():
            continue
        key = right.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"wrong": wrong, "right": right})
        if len(out) >= 4:
            break
    return out


# ----------------------------------------------------------------------
# Chart-edit: derived series + chart-type extraction
# ----------------------------------------------------------------------
CHART_EDITOR_ALLOWED_OPERATORS = {
    "moving_avg",
    "cumulative_sum",
    "percent_change",
    "linear_trend",
    "normalize_0_1",
    "log_scale",
}


def normalise_derived_series(
    items: Any, allowed_columns: List[str]
) -> List[Dict[str, Any]]:
    """Validate and clean the derived_series array returned by the LLM.

    - Drop any item whose operator is not in the allowed set.
    - Drop any item whose source_column is not in the request's column list
      (case-insensitive match).
    - Coerce params to a dict; coerce ``params.window`` to a positive int.
    - Cap the list to 4 entries to keep the chart readable.
    """
    if not isinstance(items, list):
        return []
    lowered_columns = {c.lower(): c for c in allowed_columns}
    out: List[Dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        op = str(raw.get("operator") or "").strip().lower()
        if op not in CHART_EDITOR_ALLOWED_OPERATORS:
            continue
        src = raw.get("source_column")
        canonical_src: Optional[str] = None
        if isinstance(src, str) and src.strip():
            canonical_src = lowered_columns.get(src.strip().lower())
            if canonical_src is None:
                # Unknown column — skip rather than mislead the client.
                continue
        params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
        window = params.get("window")
        if window is not None:
            try:
                window_int = int(window)
                params = {**params, "window": max(1, window_int)}
            except (TypeError, ValueError):
                params = {k: v for k, v in params.items() if k != "window"}
        label = raw.get("label")
        if not isinstance(label, str) or not label.strip():
            label = f"{op} ({canonical_src})" if canonical_src else op
        out.append(
            {
                "operator": op,
                "source_column": canonical_src,
                "params": params,
                "label": label.strip()[:80],
            }
        )
        if len(out) >= 4:
            break
    return out


def extract_chart_type(config: Dict[str, Any]) -> str:
    """Return the chart's primary series type, defaulting to ``bar``."""
    series = config.get("series") if isinstance(config, dict) else None
    if isinstance(series, list) and series and isinstance(series[0], dict):
        t = series[0].get("type")
        if isinstance(t, str) and t:
            return t
    return "bar"
