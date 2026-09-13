"""Pure helpers for per-turn conversation artifacts.

Everything here is side-effect free so it can be unit-tested without a DB:

* ``coerce_json_safe_rows``  — normalise driver values (Decimal, datetime,
  UUID, bytes) for dict *or* positional rows.
* ``build_result_snapshot``  — all-or-nothing snapshot under the row/byte caps.
* ``extract_artifact_fields`` — the explicit allowlist taken from the graph's
  ``formatted_response`` (never prompt / node_prompts / results / trace).
* ``measure_chart_payload``  — byte accounting for chart_spec + chart_config.
"""

from __future__ import annotations

import decimal
import json
from datetime import date, datetime, time as dt_time
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

RESULT_KIND_TABLE = "table"
RESULT_KIND_TEXT = "text"
RESULT_KIND_ERROR = "error"

SNAPSHOT_STORED = "stored"
SNAPSHOT_TOO_LARGE = "too_large"
SNAPSHOT_PRUNED = "pruned"
SNAPSHOT_NOT_APPLICABLE = "not_applicable"

ANSWER_MAX_BYTES = 32 * 1024
ANALYTICS_MAX_ITEMS = 20
ANALYTICS_ITEM_MAX_BYTES = 1024
ERROR_MAX_CHARS = 4000
TITLE_MAX_CHARS = 200

_METRIC_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "llm_latency_ms",
    "execution_time_ms",
    "retry_count",
    "llm_call_count",
)


def json_safe_value(value: Any) -> Any:
    """Convert common DB driver values into JSON-serialisable equivalents."""
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime, date, dt_time, UUID)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [json_safe_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_safe_value(v) for k, v in value.items()}
    return value


def coerce_json_safe_rows(rows: Optional[List[Any]]) -> List[Any]:
    """Normalise a list of rows; each row may be a mapping or a positional list."""
    out: List[Any] = []
    for row in rows or []:
        if isinstance(row, dict):
            out.append({str(k): json_safe_value(v) for k, v in row.items()})
        elif isinstance(row, (list, tuple)):
            out.append([json_safe_value(v) for v in row])
        elif hasattr(row, "keys") and hasattr(row, "items"):
            # asyncpg.Record and other mapping-likes
            out.append({str(k): json_safe_value(v) for k, v in dict(row).items()})
        else:
            out.append(json_safe_value(row))
    return out


def measure_json_bytes(obj: Any) -> int:
    """UTF-8 size of the compact JSON encoding of *obj*."""
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def build_result_snapshot(
    query_result: Optional[Dict[str, Any]],
    *,
    max_rows: int,
    max_bytes: int,
) -> Tuple[Optional[Dict[str, Any]], str, Optional[int], int]:
    """Return ``(snapshot, status, bytes, row_count)`` for one successful result.

    The snapshot keeps the whole result envelope (``columns``, ``rows``,
    ``row_count``, ``truncated``, ``cap``/``total`` meta) so the cap banner can
    be rendered faithfully on restore. It is all-or-nothing: above either cap
    the snapshot is ``None`` with status ``too_large``. An empty successful
    result is stored as an empty envelope.
    """
    result = query_result or {}
    raw_rows = result.get("rows")
    if raw_rows is None:
        raw_rows = result.get("data") or []
    row_count = len(raw_rows)
    if row_count > max_rows:
        return None, SNAPSHOT_TOO_LARGE, None, row_count

    rows = coerce_json_safe_rows(raw_rows)
    columns = [str(c) for c in (result.get("columns") or [])]
    snapshot: Dict[str, Any] = {
        "columns": columns,
        "rows": rows,
        "row_count": row_count,
    }
    for key in ("truncated", "cap", "total", "total_row_count", "capped"):
        if key in result and result[key] is not None:
            snapshot[key] = json_safe_value(result[key])

    size = measure_json_bytes(snapshot)
    if size > max_bytes:
        return None, SNAPSHOT_TOO_LARGE, size, row_count
    return snapshot, SNAPSHOT_STORED, size, row_count


def _cap_text(value: Any, max_bytes: int) -> Any:
    """Truncate a string to *max_bytes* of UTF-8 without splitting a code point."""
    if not isinstance(value, str):
        return value
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _cap_answer(answer: Any) -> Any:
    """Bound the answer payload; rich fragment arrays are kept when they fit."""
    if answer is None:
        return None
    safe = json_safe_value(answer)
    if isinstance(safe, str):
        return _cap_text(safe, ANSWER_MAX_BYTES)
    if measure_json_bytes(safe) <= ANSWER_MAX_BYTES:
        return safe
    # Too big as structured content: flatten to text and cap.
    if isinstance(safe, list):
        flat = "".join(
            str(seg.get("t") or "") if isinstance(seg, dict) else str(seg)
            for seg in safe
        )
        return _cap_text(flat, ANSWER_MAX_BYTES)
    return _cap_text(json.dumps(safe, ensure_ascii=False), ANSWER_MAX_BYTES)


def _cap_analytics(items: Any) -> Optional[List[str]]:
    if not items:
        return None
    out: List[str] = []
    for item in list(items)[:ANALYTICS_MAX_ITEMS]:
        if item is None:
            continue
        if isinstance(item, list):
            item = "".join(
                str(seg.get("t") or "") if isinstance(seg, dict) else str(seg)
                for seg in item
            )
        elif isinstance(item, dict):
            item = str(item.get("t") or json.dumps(item, ensure_ascii=False))
        out.append(_cap_text(str(item), ANALYTICS_ITEM_MAX_BYTES))
    return out or None


def _numeric_metrics(metrics: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(metrics, dict):
        return None
    out: Dict[str, Any] = {}
    for key in _METRIC_KEYS:
        val = metrics.get(key)
        if isinstance(val, bool):
            continue
        if isinstance(val, (int, float)):
            out[key] = val
    route = metrics.get("route")
    if isinstance(route, str) and route:
        out["route"] = route[:64]
    return out or None


def extract_artifact_fields(
    formatted_response: Optional[Dict[str, Any]],
    *,
    has_sql: bool,
    exec_error: Optional[str],
) -> Dict[str, Any]:
    """Build the persisted allowlist from the graph's ``formatted_response``.

    Only ``answer``, ``error``, ``metrics``, ``findings``, ``suggestions`` and
    ``followups`` are taken. ``prompt``, ``node_prompts``, ``results`` and
    ``trace`` are deliberately ignored: they carry schema context and full rows.
    Also decides the turn's ``result_kind``.
    """
    fr = formatted_response or {}
    error = exec_error or fr.get("error")
    if error is not None:
        error = _cap_text(str(error), ERROR_MAX_CHARS * 4)[:ERROR_MAX_CHARS]

    if error:
        result_kind = RESULT_KIND_ERROR
    elif has_sql:
        result_kind = RESULT_KIND_TABLE
    else:
        result_kind = RESULT_KIND_TEXT

    return {
        "result_kind": result_kind,
        "answer": _cap_answer(fr.get("answer")),
        "error": error or None,
        "metrics": _numeric_metrics(fr.get("metrics")),
        "findings": _cap_analytics(fr.get("findings")),
        "suggestions": _cap_analytics(fr.get("suggestions")),
        "followups": _cap_analytics(fr.get("followups")),
    }


def measure_chart_payload(
    chart_spec: Optional[Dict[str, Any]],
    chart_config: Optional[Dict[str, Any]],
) -> int:
    """Combined UTF-8 JSON size of the chart baseline."""
    return measure_json_bytes({"spec": chart_spec or {}, "config": chart_config or {}})


def conversation_title_from_question(question: Optional[str]) -> str:
    text = " ".join(str(question or "").split())
    if not text:
        return "Conversation"
    if len(text) <= TITLE_MAX_CHARS:
        return text
    return text[: TITLE_MAX_CHARS - 1].rstrip() + "…"
