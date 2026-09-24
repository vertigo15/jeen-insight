"""Trust boundary for model-produced ECharts edit configurations.

The chart editor is intentionally less permissive than ECharts.  It accepts
the small, JSON-only subset used by Jeen's deterministic chart builder and
verifies that a view/style edit preserves the chart's existing data.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional


MAX_CONFIG_BYTES = 512_000
MAX_CONFIG_DEPTH = 14
MAX_CONFIG_NODES = 20_000
MAX_STRING_CHARS = 4_096
MAX_TOTAL_STRING_CHARS = 200_000
MAX_KEY_CHARS = 80
MAX_SERIES = 64
MAX_DATA_ITEMS = 10_000

_SAFE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_DANGEROUS_KEYS = {
    "__proto__",
    "constructor",
    "prototype",
    "dataset",
    "transform",
    "graphic",
    "renderitem",
    "extracsstext",
    "rich",
}
_IMMUTABLE_EXISTING_KEYS = {
    "geo",
    "geoindex",
    "map",
    "maptype",
    "jeenmap",
    "jeenosmmap",
}
_DANGEROUS_STRING = re.compile(
    r"(?:<\s*/?\s*[a-z!]|javascript\s*:|data\s*:|vbscript\s*:|"
    r"https?\s*:|file\s*:|url\s*\(|image\s*://|path\s*://|"
    r"\bfunction\s*\(|=>|\$\{)",
    re.IGNORECASE,
)
_ALLOWED_SERIES_TYPES = {
    "bar", "line", "pie", "scatter", "gauge", "heatmap", "map",
}
_CARTESIAN_TYPES = {"bar", "line", "scatter", "heatmap"}


@dataclass(frozen=True)
class ChartEditValidation:
    ok: bool
    code: Optional[str] = None
    message: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


class ChartConfigLimitError(ValueError):
    """Raised when a request config exceeds the bounded JSON contract."""


def _failure(code: str, message: str, **details: Any) -> ChartEditValidation:
    return ChartEditValidation(False, code=code, message=message, details=details)


def assert_bounded_chart_config(config: Any) -> None:
    """Reject oversized, deeply nested, non-finite, or non-JSON config input."""
    try:
        encoded = json.dumps(
            config,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ChartConfigLimitError("Chart config must contain finite JSON values.") from exc
    if len(encoded.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ChartConfigLimitError("Chart config is too large.")

    nodes = 0
    string_chars = 0

    def walk(value: Any, depth: int) -> None:
        nonlocal nodes, string_chars
        if depth > MAX_CONFIG_DEPTH:
            raise ChartConfigLimitError("Chart config is nested too deeply.")
        nodes += 1
        if nodes > MAX_CONFIG_NODES:
            raise ChartConfigLimitError("Chart config contains too many values.")
        if isinstance(value, str):
            if len(value) > MAX_STRING_CHARS:
                raise ChartConfigLimitError("Chart config contains an overlong string.")
            string_chars += len(value)
            if string_chars > MAX_TOTAL_STRING_CHARS:
                raise ChartConfigLimitError("Chart config contains too much text.")
        elif isinstance(value, float) and not math.isfinite(value):
            raise ChartConfigLimitError("Chart config contains a non-finite number.")
        elif isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ChartConfigLimitError("Chart config keys must be strings.")
                walk(key, depth + 1)
                walk(child, depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, depth + 1)
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise ChartConfigLimitError("Chart config contains a non-JSON value.")

    walk(config, 0)


def _axis_objects(value: Any) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    return []


def _category_axis(config: dict) -> tuple[Optional[str], list[Any]]:
    for key in ("xAxis", "yAxis"):
        for axis in _axis_objects(config.get(key)):
            data = axis.get("data")
            if axis.get("type") == "category" or isinstance(data, list):
                return key, data if isinstance(data, list) else []
    return None, []


def _item_value(item: Any) -> Any:
    return item.get("value") if isinstance(item, dict) else item


def _item_name(item: Any) -> Any:
    return item.get("name") if isinstance(item, dict) else None


def _normalise_data_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return tuple(_normalise_data_value(item) for item in value)
    if isinstance(value, dict):
        return tuple(
            sorted(
                (key, _normalise_data_value(child))
                for key, child in value.items()
                if key in {"name", "value"}
            )
        )
    return repr(value)


def _series_data_records(config: dict) -> list[tuple[Any, ...]]:
    series = config.get("series")
    if not isinstance(series, list):
        return []
    _, categories = _category_axis(config)
    use_series_name = len(series) > 1
    records: list[tuple[Any, ...]] = []
    for index, item in enumerate(series):
        if not isinstance(item, dict):
            continue
        series_type = str(item.get("type") or "").lower()
        series_name = item.get("name") if use_series_name else None
        data = item.get("data")
        if not isinstance(data, list):
            continue
        if series_type == "pie":
            for point_index, point in enumerate(data):
                name = _item_name(point)
                if name is None and point_index < len(categories):
                    name = categories[point_index]
                records.append((series_name, name, _normalise_data_value(_item_value(point))))
        elif categories and series_type in {"bar", "line"}:
            for point_index, point in enumerate(data):
                category = categories[point_index] if point_index < len(categories) else None
                records.append(
                    (series_name, category, _normalise_data_value(_item_value(point)))
                )
        else:
            for point in data:
                records.append((series_name, _normalise_data_value(_item_value(point))))
    return records


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, float) and isinstance(right, float):
        return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-12)
    if isinstance(left, tuple) and isinstance(right, tuple):
        return len(left) == len(right) and all(
            _values_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _records_equal(left: Iterable[tuple[Any, ...]], right: Iterable[tuple[Any, ...]]) -> bool:
    left_sorted = sorted(left, key=repr)
    right_sorted = sorted(right, key=repr)
    return len(left_sorted) == len(right_sorted) and all(
        _values_equal(a, b) for a, b in zip(left_sorted, right_sorted)
    )


def _validate_safe_tree(
    value: Any,
    current: Any = None,
    path: str = "$",
) -> Optional[ChartEditValidation]:
    if isinstance(value, dict):
        for key, child in value.items():
            folded = key.casefold()
            current_child = current.get(key) if isinstance(current, dict) else None
            if folded in _IMMUTABLE_EXISTING_KEYS:
                if not isinstance(current, dict) or key not in current or current_child != child:
                    return _failure(
                        "unsafe_surface",
                        "The proposed edit changed a protected map surface.",
                        path=f"{path}.{key}",
                    )
                # Server-built map bindings are safe only as an opaque,
                # unchanged value. Do not recursively reinterpret them.
                continue
            if (
                len(key) > MAX_KEY_CHARS
                or not _SAFE_KEY.fullmatch(key)
                or folded in _DANGEROUS_KEYS
                or "url" in folded
                or "image" in folded
            ):
                return _failure(
                    "unsafe_surface",
                    "The proposed edit used an unsupported chart surface.",
                    path=f"{path}.{key}",
                )
            if folded == "formatter":
                if not isinstance(child, str) or _DANGEROUS_STRING.search(child):
                    return _failure(
                        "unsafe_formatter",
                        "The proposed edit used an unsafe formatter.",
                        path=f"{path}.{key}",
                    )
            nested = _validate_safe_tree(child, current_child, f"{path}.{key}")
            if nested:
                return nested
    elif isinstance(value, list):
        for index, child in enumerate(value):
            current_child = (
                current[index]
                if isinstance(current, list) and index < len(current)
                else None
            )
            nested = _validate_safe_tree(child, current_child, f"{path}[{index}]")
            if nested:
                return nested
    elif isinstance(value, str) and _DANGEROUS_STRING.search(value):
        return _failure(
            "unsafe_string",
            "The proposed edit contained unsafe markup or an external resource.",
            path=path,
        )
    return None


def _validate_chart_shape(config: dict) -> Optional[ChartEditValidation]:
    series = config.get("series")
    if not isinstance(series, list) or not series or len(series) > MAX_SERIES:
        return _failure(
            "invalid_series",
            "The proposed chart must contain a bounded series list.",
        )
    series_types: set[str] = set()
    for index, item in enumerate(series):
        if not isinstance(item, dict):
            return _failure("invalid_series", "Every chart series must be an object.")
        series_type = str(item.get("type") or "").strip().lower()
        if series_type not in _ALLOWED_SERIES_TYPES:
            return _failure(
                "unsupported_chart_type",
                "The proposed chart type is not supported for chat edits.",
                series_index=index,
                series_type=series_type,
            )
        series_types.add(series_type)
        data = item.get("data")
        if data is not None and (
            not isinstance(data, list) or len(data) > MAX_DATA_ITEMS
        ):
            return _failure(
                "invalid_series_data",
                "The proposed series data is not a bounded list.",
                series_index=index,
            )

    needs_cartesian_axes = any(
        str(item.get("type") or "").strip().lower() in _CARTESIAN_TYPES
        and item.get("coordinateSystem") != "geo"
        for item in series
    )
    if needs_cartesian_axes:
        if not _axis_objects(config.get("xAxis")) or not _axis_objects(config.get("yAxis")):
            return _failure(
                "missing_axes",
                "Cartesian charts require both xAxis and yAxis.",
            )

    _, categories = _category_axis(config)
    if categories:
        category_counts = Counter(map(str, categories))
        if any(count > 1 for count in category_counts.values()):
            # Duplicate categories are valid after aggregation; this check is
            # intentionally only about array alignment, not uniqueness.
            pass
        for index, item in enumerate(series):
            if str(item.get("type") or "").lower() not in {"bar", "line"}:
                continue
            data = item.get("data")
            if isinstance(data, list) and len(data) != len(categories):
                return _failure(
                    "category_series_mismatch",
                    "Category and series arrays must stay aligned.",
                    series_index=index,
                    categories=len(categories),
                    values=len(data),
                )
    return None


def validate_chart_edit(
    current_config: Dict[str, Any],
    candidate_config: Dict[str, Any],
) -> ChartEditValidation:
    """Validate safety, shape, and data preservation for a model chart edit."""
    try:
        assert_bounded_chart_config(current_config)
        assert_bounded_chart_config(candidate_config)
    except ChartConfigLimitError as exc:
        return _failure("invalid_config", str(exc))
    if not isinstance(candidate_config, dict):
        return _failure("invalid_config", "The proposed chart config must be an object.")

    unsafe = _validate_safe_tree(candidate_config, current_config)
    if unsafe:
        return unsafe
    invalid_shape = _validate_chart_shape(candidate_config)
    if invalid_shape:
        return invalid_shape

    current_records = _series_data_records(current_config)
    candidate_records = _series_data_records(candidate_config)
    if not _records_equal(current_records, candidate_records):
        return _failure(
            "data_changed",
            "Chart chat can restyle or reorder existing values, but cannot change chart data.",
        )
    return ChartEditValidation(True)
