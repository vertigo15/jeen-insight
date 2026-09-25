"""Typed, allowlisted operations for chart-only LLM edits.

The model never receives rows and never returns an ECharts option.  This module
owns the contract that sits between the model and the browser, including the
manifest-aware checks that Pydantic cannot perform on an operation in isolation.
"""

from __future__ import annotations

import math
import re
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from src.api.llm_json import CHART_EDITOR_ALLOWED_OPERATORS


class _StrictOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SetColorOperation(_StrictOperation):
    op: Literal["set_color"]
    target: str = Field(min_length=1, max_length=280)
    color: str = Field(min_length=1, max_length=32)


class SetStyleOperation(_StrictOperation):
    op: Literal["set_style"]
    target: str = Field(min_length=1, max_length=280)
    path: str = Field(min_length=1, max_length=64)
    value: Union[str, int, float, bool]


class SetToggleOperation(_StrictOperation):
    op: Literal["set_toggle"]
    key: Literal["dataLabels", "legend", "dataZoom"]
    value: bool
    target: Optional[str] = Field(default=None, min_length=1, max_length=280)


class SetFormatOperation(_StrictOperation):
    op: Literal["set_format"]
    kind: Literal["number", "currency", "percent"]
    compact: bool = True
    symbol: str = Field(default="", max_length=4)

    @model_validator(mode="after")
    def _currency_symbol_only(self) -> "SetFormatOperation":
        if self.kind != "currency" and self.symbol:
            raise ValueError("symbol is allowed only for currency formatting")
        if self.symbol not in {
            "",
            "$",
            "€",
            "£",
            "₪",
            "¥",
            "₹",
            "₩",
            "₽",
            "₺",
            "₴",
            "₫",
            "₱",
            "฿",
            "₦",
            "R$",
        }:
            raise ValueError("symbol is not an allowlisted currency symbol")
        return self


class RenameSeriesOperation(_StrictOperation):
    op: Literal["rename_series"]
    target: str = Field(min_length=1, max_length=280)
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        if any(char in value for char in "<>\r\n\x00"):
            raise ValueError("series name contains unsafe characters")
        return value


class HideSeriesOperation(_StrictOperation):
    op: Literal["hide_series"]
    target: str = Field(min_length=1, max_length=280)
    hidden: bool


class AddOverlayOperation(_StrictOperation):
    op: Literal["add_overlay"]
    operator: Literal[
        "moving_avg",
        "cumulative_sum",
        "percent_change",
        "linear_trend",
        "normalize_0_1",
        "log_scale",
    ]
    source_column: str = Field(min_length=1, max_length=256)
    params: dict[str, Any] = Field(default_factory=dict)
    label: str = Field(min_length=1, max_length=80)


class RemoveOverlayOperation(_StrictOperation):
    op: Literal["remove_overlay"]
    operator: Optional[
        Literal[
            "moving_avg",
            "cumulative_sum",
            "percent_change",
            "linear_trend",
            "normalize_0_1",
            "log_scale",
        ]
    ] = None
    label: Optional[str] = Field(default=None, min_length=1, max_length=80)


class SetSortOperation(_StrictOperation):
    op: Literal["set_sort"]
    direction: Literal["asc", "desc", "none"]


ChartTypeName = Literal[
    "bar",
    "line",
    "area",
    "pie",
    "donut",
    "scatter",
    "horizontal_bar",
    "stacked_bar",
    "stacked_area",
    "combo",
    "heatmap",
    "gauge",
]

# Types the browser can flip locally between compatible cartesian series. Any
# other target type is rebuilt deterministically by the server from cached rows.
LOCAL_CHART_TYPES = frozenset({"bar", "line", "area"})


class SetChartTypeOperation(_StrictOperation):
    op: Literal["set_chart_type"]
    chart_type: ChartTypeName


class SetPaletteOperation(_StrictOperation):
    """Ordered colour list for the whole chart (pie slices, multi-series)."""

    op: Literal["set_palette"]
    colors: list[str] = Field(min_length=1, max_length=12)

    @field_validator("colors")
    @classmethod
    def _bounded_colors(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) or not 1 <= len(item) <= 32 for item in value):
            raise ValueError("palette colors must be short strings")
        return value


class SetStackOperation(_StrictOperation):
    op: Literal["set_stack"]
    stacked: bool


class SetBindingOperation(_StrictOperation):
    op: Literal["set_binding"]
    x: Optional[str] = Field(default=None, min_length=1, max_length=256)
    y: Optional[str] = Field(default=None, min_length=1, max_length=256)
    series: Optional[str] = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def _has_binding(self) -> "SetBindingOperation":
        if not {"x", "y", "series"}.intersection(self.model_fields_set):
            raise ValueError("set_binding requires x, y, or series")
        return self


_Number = Union[int, float]
_SAFE_LABEL_RE = re.compile(r"^[^<>\r\n\x00]{1,80}$")


def _safe_label(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not _SAFE_LABEL_RE.fullmatch(value):
        raise ValueError("label contains unsafe characters or is too long")
    return value


class _ScenarioBase(_StrictOperation):
    """What-if edits never touch the real series; they add a labelled copy."""

    target: str = Field(min_length=1, max_length=280)
    label: Optional[str] = Field(default=None, max_length=80)

    @field_validator("label")
    @classmethod
    def _label_ok(cls, value: Optional[str]) -> Optional[str]:
        return _safe_label(value)


class ScenarioSetPointOperation(_ScenarioBase):
    op: Literal["scenario_set_point"]
    category: str = Field(min_length=1, max_length=160)
    value: _Number


class ScenarioScaleOperation(_ScenarioBase):
    op: Literal["scenario_scale"]
    percent: Optional[_Number] = None
    factor: Optional[_Number] = None
    from_category: Optional[str] = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def _one_of(self) -> "ScenarioScaleOperation":
        if (self.percent is None) == (self.factor is None):
            raise ValueError("scenario_scale needs exactly one of percent or factor")
        return self


class ScenarioShiftOperation(_ScenarioBase):
    op: Literal["scenario_shift"]
    delta: _Number
    from_category: Optional[str] = Field(default=None, min_length=1, max_length=160)


class ScenarioClearOperation(_StrictOperation):
    op: Literal["scenario_clear"]
    label: Optional[str] = Field(default=None, max_length=80)

    @field_validator("label")
    @classmethod
    def _label_ok(cls, value: Optional[str]) -> Optional[str]:
        return _safe_label(value)


class AddReferenceLineOperation(_StrictOperation):
    op: Literal["add_reference_line"]
    axis: Literal["y", "x"] = "y"
    value: Optional[_Number] = None
    stat: Optional[Literal["avg", "min", "max", "median"]] = None
    target: Optional[str] = Field(default=None, min_length=1, max_length=280)
    label: str = Field(min_length=1, max_length=80)

    @field_validator("label")
    @classmethod
    def _label_ok(cls, value: str) -> str:
        return _safe_label(value) or value

    @model_validator(mode="after")
    def _one_of(self) -> "AddReferenceLineOperation":
        if (self.value is None) == (self.stat is None):
            raise ValueError("add_reference_line needs exactly one of value or stat")
        if self.axis == "x" and self.stat is not None:
            raise ValueError("stat reference lines are only meaningful on the value axis")
        return self


class RemoveReferenceLineOperation(_StrictOperation):
    op: Literal["remove_reference_line"]
    label: Optional[str] = Field(default=None, max_length=80)

    @field_validator("label")
    @classmethod
    def _label_ok(cls, value: Optional[str]) -> Optional[str]:
        return _safe_label(value)


class HighlightPredicate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    op: Literal["lt", "lte", "gt", "gte", "eq", "between"]
    value: _Number
    value2: Optional[_Number] = None

    @model_validator(mode="after")
    def _between_bounds(self) -> "HighlightPredicate":
        if self.op == "between" and self.value2 is None:
            raise ValueError("between needs value2")
        if self.op != "between" and self.value2 is not None:
            raise ValueError("value2 is only allowed for between")
        return self


class HighlightPointsOperation(_StrictOperation):
    op: Literal["highlight_points"]
    target: str = Field(min_length=1, max_length=280)
    categories: Optional[list[str]] = Field(default=None, min_length=1, max_length=12)
    predicate: Optional[HighlightPredicate] = None
    color: Optional[str] = Field(default=None, min_length=1, max_length=32)
    label: Optional[str] = Field(default=None, max_length=80)

    @field_validator("label")
    @classmethod
    def _label_ok(cls, value: Optional[str]) -> Optional[str]:
        return _safe_label(value)

    @field_validator("categories")
    @classmethod
    def _categories_ok(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is not None and any(not isinstance(item, str) or not 1 <= len(item) <= 160 for item in value):
            raise ValueError("categories must be short strings")
        return value

    @model_validator(mode="after")
    def _one_of(self) -> "HighlightPointsOperation":
        if (self.categories is None) == (self.predicate is None):
            raise ValueError("highlight_points needs exactly one of categories or predicate")
        return self


class ClearHighlightsOperation(_StrictOperation):
    op: Literal["clear_highlights"]


MAX_SCENARIOS = 2


ChartOperation = Annotated[
    Union[
        SetColorOperation,
        SetStyleOperation,
        SetToggleOperation,
        SetFormatOperation,
        RenameSeriesOperation,
        HideSeriesOperation,
        AddOverlayOperation,
        RemoveOverlayOperation,
        SetSortOperation,
        SetChartTypeOperation,
        SetStackOperation,
        SetBindingOperation,
        SetPaletteOperation,
        ScenarioSetPointOperation,
        ScenarioScaleOperation,
        ScenarioShiftOperation,
        ScenarioClearOperation,
        AddReferenceLineOperation,
        RemoveReferenceLineOperation,
        HighlightPointsOperation,
        ClearHighlightsOperation,
    ],
    Field(discriminator="op"),
]


class ChartOperationEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operations: list[ChartOperation] = Field(default_factory=list, max_length=12)
    notes: Optional[str] = Field(default=None, max_length=300)
    out_of_scope: bool = False
    reason_code: Optional[str] = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")


OPERATION_ENVELOPE_ADAPTER = TypeAdapter(ChartOperationEnvelope)

_TARGET_RE = re.compile(r"^(all|role:.+|name:.+|id:.+)$", re.DOTALL)
_COLOR_RE = re.compile(
    r"^(?:#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})|"
    r"rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}"
    r"(?:\s*,\s*(?:0(?:\.\d+)?|1(?:\.0+)?))?\s*\)|"
    r"hsla?\(\s*\d{1,3}\s*,\s*\d{1,3}%\s*,\s*\d{1,3}%"
    r"(?:\s*,\s*(?:0(?:\.\d+)?|1(?:\.0+)?))?\s*\))$"
)
_SAFE_TEMPLATE_RE = re.compile(
    r"^(?:[^{}]|\{(?:a|b|c|d|value|seriesName|name)\}){1,80}$"
)
_PROTECTED_ROLES = {
    "interval_base",
    "interval_bound",
    "lower_bound",
    "upper_bound",
    "helper",
}
_NO_AXIS_TYPES = {"pie", "donut", "gauge"}
_NO_STACK_TYPES = {"pie", "donut", "gauge", "scatter", "heatmap"}
# Palettes are ignored by the renderer for these (their colour comes from a
# visualMap or the gauge arc), so the contract tells the model up front.
_PALETTE_EXEMPT_TYPES = {"heatmap", "gauge", "map", "osm_map"}
# ML result charts carry role-based colours and a fixed layout, so palette and
# structural operations are ignored for them.
_ML_FORBIDDEN = {
    "set_chart_type",
    "set_sort",
    "set_stack",
    "set_binding",
    "set_palette",
    # A what-if on a model output is a new analysis, not a chart edit.
    "scenario_set_point",
    "scenario_scale",
    "scenario_shift",
    "scenario_clear",
}
_SCENARIO_OPS = (ScenarioSetPointOperation, ScenarioScaleOperation, ScenarioShiftOperation)

_STYLE_RULES: dict[str, tuple[str, Any]] = {
    "itemStyle.color": ("color", None),
    "itemStyle.borderColor": ("color", None),
    "itemStyle.opacity": ("number", (0, 1)),
    "itemStyle.borderWidth": ("number", (0, 12)),
    "lineStyle.color": ("color", None),
    "lineStyle.width": ("number", (0, 12)),
    "lineStyle.opacity": ("number", (0, 1)),
    "lineStyle.type": ("enum", {"solid", "dashed", "dotted"}),
    "areaStyle.color": ("color", None),
    "areaStyle.opacity": ("number", (0, 1)),
    "opacity": ("number", (0, 1)),
    "symbolSize": ("number", (1, 80)),
    "symbol": (
        "enum",
        {"circle", "rect", "roundRect", "triangle", "diamond", "pin", "arrow", "none"},
    ),
    "smooth": ("bool", None),
    "label.show": ("bool", None),
    "label.color": ("color", None),
    "label.position": (
        "enum",
        {"top", "bottom", "left", "right", "inside", "insideTop", "insideBottom"},
    ),
    "label.fontSize": ("number", (8, 40)),
    "label.formatter": ("template", None),
}
_INTERVAL_STYLE_PATHS = {
    "areaStyle.color",
    "areaStyle.opacity",
    "itemStyle.color",
    "itemStyle.opacity",
}


class OperationContractError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _manifest_dict(manifest: Any) -> dict[str, Any]:
    if isinstance(manifest, BaseModel):
        return manifest.model_dump(by_alias=True, mode="json", exclude_none=True)
    return manifest if isinstance(manifest, dict) else {}


def _manifest_series(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in manifest.get("series", []) if isinstance(item, dict)]


def _series_role(item: dict[str, Any]) -> str:
    return str(item.get("jeenRole") or item.get("jeen_role") or "").strip()


def _is_protected(item: dict[str, Any]) -> bool:
    return bool(item.get("locked")) or _series_role(item).casefold() in _PROTECTED_ROLES


def _target_matches(target: str, series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(target, str) or not _TARGET_RE.fullmatch(target):
        raise OperationContractError(
            "invalid_target",
            "Targets must be all, role:<role>, name:<name>, or id:<id>.",
        )
    if target == "all":
        matches = [item for item in series if not _is_protected(item)]
    else:
        kind, value = target.split(":", 1)
        value = value.strip()
        if not value or value.isdigit():
            raise OperationContractError("invalid_target", "Numeric series indices are not allowed.")
        key = {"role": "jeenRole", "name": "name", "id": "id"}[kind]
        matches = []
        for item in series:
            candidate = _series_role(item) if kind == "role" else item.get(key)
            if str(candidate or "").casefold() == value.casefold():
                matches.append(item)
    if not matches:
        raise OperationContractError(
            "unknown_target", f"Target {target!r} does not match an editable series."
        )
    if any(_is_protected(item) for item in matches):
        raise OperationContractError(
            "protected_series", "Helper and interval-band series cannot be edited."
        )
    return matches


def _validate_color(color: str) -> None:
    if not _COLOR_RE.fullmatch(color.strip()):
        raise OperationContractError(
            "invalid_color", "Colors must be safe hex, rgb/rgba, or hsl/hsla values."
        )
    numbers = [int(value) for value in re.findall(r"\d+", color)[:3]]
    if color.lower().startswith("rgb") and any(value > 255 for value in numbers):
        raise OperationContractError("invalid_color", "RGB components must be at most 255.")
    if color.lower().startswith("hsl") and (
        numbers[0] > 360 or numbers[1] > 100 or numbers[2] > 100
    ):
        raise OperationContractError("invalid_color", "HSL components are out of range.")


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _validate_style(path: str, value: Any) -> None:
    rule = _STYLE_RULES.get(path)
    if rule is None:
        raise OperationContractError("invalid_style_path", f"Style path {path!r} is not allowed.")
    kind, bounds = rule
    if kind == "number":
        number = _finite_number(value)
        if number is None or not bounds[0] <= number <= bounds[1]:
            raise OperationContractError("invalid_style_value", f"{path} is outside its safe range.")
    elif kind == "enum" and value not in bounds:
        raise OperationContractError("invalid_style_value", f"{path} has an unsupported value.")
    elif kind == "color":
        if not isinstance(value, str):
            raise OperationContractError("invalid_style_value", f"{path} must be a color.")
        _validate_color(value)
    elif kind == "bool" and not isinstance(value, bool):
        raise OperationContractError("invalid_style_value", f"{path} must be true or false.")
    elif kind == "template":
        if not isinstance(value, str) or not _SAFE_TEMPLATE_RE.fullmatch(value):
            raise OperationContractError(
                "invalid_template", "Label templates may contain only text and safe ECharts tokens."
            )


def _manifest_categories(manifest: dict[str, Any]) -> tuple[list[str], bool]:
    """Category labels of the manifest's category axis and whether the list is complete.

    The browser sends every label when the axis is small (``complete: true``)
    and only a sample otherwise; scenario categories are validated only in the
    former case so a long axis never causes a false rejection.
    """

    axes = manifest.get("axes") or manifest.get("axis") or {}
    entries: list[dict[str, Any]] = []
    if isinstance(axes, dict):
        for key in ("x", "y"):
            value = axes.get(key)
            entries.extend(item for item in (value if isinstance(value, list) else [value]) if isinstance(item, dict))
    elif isinstance(axes, list):
        entries.extend(item for item in axes if isinstance(item, dict))
    for entry in entries:
        categories = entry.get("categories")
        if not isinstance(categories, dict):
            continue
        values = categories.get("values")
        if isinstance(values, list) and categories.get("complete", True):
            return [str(value) for value in values], True
        if isinstance(categories.get("sample"), list):
            return [str(value) for value in categories["sample"]], False
    return [], False


def _column_sets(manifest: dict[str, Any]) -> tuple[set[str], set[str]]:
    all_columns: set[str] = set()
    numeric: set[str] = set()
    raw_columns = manifest.get("columns") or manifest.get("sql_columns") or []
    for column in raw_columns:
        if isinstance(column, str):
            all_columns.add(column)
            continue
        if not isinstance(column, dict):
            continue
        name = column.get("name")
        if not isinstance(name, str) or not name:
            continue
        all_columns.add(name)
        ctype = str(column.get("type") or "").casefold()
        if any(token in ctype for token in ("int", "float", "double", "decimal", "numeric", "number")):
            numeric.add(name)
    spec = manifest.get("chart_spec") or manifest.get("chartSpec") or {}
    if isinstance(spec, dict):
        for key in ("x", "series"):
            value = spec.get(key)
            if isinstance(value, str):
                all_columns.add(value)
        values = spec.get("y")
        values = [values] if isinstance(values, str) else values
        for value in values if isinstance(values, list) else []:
            if isinstance(value, str):
                all_columns.add(value)
                numeric.add(value)
    return all_columns, numeric


def _validate_overlay(operation: AddOverlayOperation, columns: set[str]) -> None:
    if operation.operator not in CHART_EDITOR_ALLOWED_OPERATORS:
        raise OperationContractError("invalid_overlay", "Overlay operator is not allowed.")
    if operation.source_column not in columns:
        raise OperationContractError(
            "unknown_column", "Overlay source_column is not present in the chart manifest."
        )
    allowed_params = {"window"} if operation.operator == "moving_avg" else set()
    unknown = set(operation.params) - allowed_params
    if unknown:
        raise OperationContractError("invalid_overlay_params", "Overlay params are not allowed.")
    if operation.operator == "moving_avg":
        window = operation.params.get("window", 3)
        if isinstance(window, bool) or not isinstance(window, int) or not 2 <= window <= 365:
            raise OperationContractError(
                "invalid_overlay_params", "moving_avg window must be an integer from 2 to 365."
            )
        operation.params["window"] = window
    elif operation.params:
        raise OperationContractError(
            "invalid_overlay_params", "This overlay operator does not accept params."
        )


def _validate_categories(
    requested: list[str], categories: list[str], complete: bool
) -> None:
    if not complete:
        return
    known = {value.casefold() for value in categories}
    missing = [value for value in requested if value.casefold() not in known]
    if missing:
        raise OperationContractError(
            "unknown_category",
            f"Category {missing[0]!r} is not on the chart axis.",
        )


def _validate_scenario(
    operation: Any, categories: list[str], complete: bool
) -> None:
    if isinstance(operation, ScenarioSetPointOperation):
        if _finite_number(operation.value) is None:
            raise OperationContractError("invalid_scenario", "Scenario value must be a finite number.")
        _validate_categories([operation.category], categories, complete)
    elif isinstance(operation, ScenarioScaleOperation):
        if operation.percent is not None:
            percent = _finite_number(operation.percent)
            if percent is None or not -100 <= percent <= 1000:
                raise OperationContractError(
                    "invalid_scenario", "Scenario percent must be between -100 and 1000."
                )
        if operation.factor is not None:
            factor = _finite_number(operation.factor)
            if factor is None or not 0 <= factor <= 20:
                raise OperationContractError(
                    "invalid_scenario", "Scenario factor must be between 0 and 20."
                )
        if operation.from_category:
            _validate_categories([operation.from_category], categories, complete)
    elif isinstance(operation, ScenarioShiftOperation):
        if _finite_number(operation.delta) is None:
            raise OperationContractError("invalid_scenario", "Scenario delta must be a finite number.")
        if operation.from_category:
            _validate_categories([operation.from_category], categories, complete)


def validate_operation_envelope(
    envelope: ChartOperationEnvelope,
    *,
    chart_kind: str,
    manifest: Any,
) -> ChartOperationEnvelope:
    """Validate parsed operations against the compact browser manifest.

    Validation is all-or-nothing: one unsafe or unknown operation rejects the
    complete model response so the browser never applies a misleading subset.
    """

    manifest_dict = _manifest_dict(manifest)
    series = _manifest_series(manifest_dict)
    columns, numeric_columns = _column_sets(manifest_dict)
    categories, categories_complete = _manifest_categories(manifest_dict)
    validated_operations = []
    skipped_ml_operations = []

    # Sort/stack are judged against the type the chart will have after this
    # answer, so "stacked bar" from a pie is fine while "stack the pie" is not.
    spec = manifest_dict.get("chart_spec") or manifest_dict.get("chartSpec") or {}
    effective_type = str(spec.get("chart_type") or "").casefold() if isinstance(spec, dict) else ""
    for operation in envelope.operations:
        if isinstance(operation, SetChartTypeOperation):
            effective_type = operation.chart_type
    scenario_count = sum(
        1 for item in manifest_dict.get("overlays", [])
        if isinstance(item, dict) and item.get("kind") == "scenario"
    )

    for operation in envelope.operations:
        if chart_kind in {"ml_band", "ml_basic"} and operation.op in _ML_FORBIDDEN:
            skipped_ml_operations.append(operation.op)
            continue
        if isinstance(operation, SetSortOperation) and effective_type in _NO_AXIS_TYPES:
            raise OperationContractError(
                "incompatible_sort", "Sorting does not apply to this chart type."
            )
        if isinstance(operation, SetStackOperation) and effective_type in _NO_STACK_TYPES:
            raise OperationContractError(
                "incompatible_stack", "Stacking does not apply to this chart type."
            )
        if isinstance(operation, SetPaletteOperation) and effective_type in _PALETTE_EXEMPT_TYPES:
            raise OperationContractError(
                "palette_not_applicable",
                "Heatmaps, gauges, and maps take their colours from the value scale, not a palette.",
            )

        target = getattr(operation, "target", None)
        if target is not None:
            matches = _target_matches(target, series)
            if any(_series_role(item).casefold() == "interval" for item in matches):
                if not isinstance(operation, (SetColorOperation, SetStyleOperation)):
                    raise OperationContractError(
                        "protected_series",
                        "Confidence intervals allow only safe fill color and opacity styles.",
                    )
            if target != "all":
                kind, _ = target.split(":", 1)
                exact = (
                    _series_role(matches[0])
                    if kind == "role"
                    else str(matches[0].get(kind) or "")
                )
                operation.target = f"{kind}:{exact}"

        if isinstance(operation, SetColorOperation):
            _validate_color(operation.color)
        elif isinstance(operation, SetPaletteOperation):
            for color in operation.colors:
                _validate_color(color)
        elif isinstance(operation, SetStyleOperation):
            _validate_style(operation.path, operation.value)
            if any(_series_role(item).casefold() == "interval" for item in matches):
                if operation.path not in _INTERVAL_STYLE_PATHS:
                    raise OperationContractError(
                        "protected_series",
                        "Confidence intervals allow only safe fill color and opacity styles.",
                    )
        elif isinstance(operation, AddOverlayOperation):
            _validate_overlay(operation, columns)
        elif isinstance(operation, RemoveOverlayOperation):
            overlays = [item for item in manifest_dict.get("overlays", []) if isinstance(item, dict)]
            if operation.operator or operation.label:
                matched = any(
                    (not operation.operator or item.get("operator") == operation.operator)
                    and (not operation.label or item.get("label") == operation.label)
                    for item in overlays
                )
                if not matched:
                    raise OperationContractError(
                        "unknown_overlay", "The requested overlay is not present in the manifest."
                    )
        elif isinstance(operation, _SCENARIO_OPS):
            scenario_count += 1
            if scenario_count > MAX_SCENARIOS:
                raise OperationContractError(
                    "too_many_scenarios",
                    f"At most {MAX_SCENARIOS} scenarios can be shown at once.",
                )
            _validate_scenario(operation, categories, categories_complete)
        elif isinstance(operation, ScenarioClearOperation):
            scenario_count = 0
            if operation.label and not any(
                isinstance(item, dict)
                and item.get("kind") == "scenario"
                and item.get("label") == operation.label
                for item in manifest_dict.get("overlays", [])
            ):
                raise OperationContractError(
                    "unknown_overlay", "The requested scenario is not present in the manifest."
                )
        elif isinstance(operation, AddReferenceLineOperation):
            if operation.value is not None and _finite_number(operation.value) is None:
                raise OperationContractError("invalid_scenario", "Reference line value must be a finite number.")
        elif isinstance(operation, RemoveReferenceLineOperation):
            annotations = manifest_dict.get("annotations") or {}
            lines = annotations.get("referenceLines", []) if isinstance(annotations, dict) else []
            if operation.label and not any(
                isinstance(item, dict) and item.get("label") == operation.label for item in lines
            ):
                raise OperationContractError(
                    "unknown_overlay", "The requested reference line is not present in the manifest."
                )
        elif isinstance(operation, HighlightPointsOperation):
            if operation.color is not None:
                _validate_color(operation.color)
            if operation.categories is not None:
                _validate_categories(operation.categories, categories, categories_complete)
            if operation.predicate is not None:
                for value in (operation.predicate.value, operation.predicate.value2):
                    if value is not None and _finite_number(value) is None:
                        raise OperationContractError(
                            "invalid_scenario", "Highlight thresholds must be finite numbers."
                        )
        elif isinstance(operation, SetBindingOperation):
            requested = [value for value in (operation.x, operation.y, operation.series) if value]
            unknown = [value for value in requested if value not in columns]
            if unknown:
                raise OperationContractError(
                    "unknown_column", "A requested binding is not present in the SQL columns."
                )
            if operation.y and numeric_columns and operation.y not in numeric_columns:
                raise OperationContractError(
                    "invalid_measure", "The Y binding must be a numeric SQL column."
                )
        validated_operations.append(operation)

    if skipped_ml_operations:
        if not validated_operations and not envelope.out_of_scope:
            raise OperationContractError(
                "operation_not_allowed_for_chart_kind",
                "ML result charts do not allow type, sort, stack, palette, or binding changes.",
            )
        envelope.operations = validated_operations
        locked_note = "Analysis-controlled layout changes were ignored."
        envelope.notes = (
            f"{envelope.notes.rstrip()} {locked_note}"
            if envelope.notes
            else locked_note
        )

    if envelope.out_of_scope and envelope.operations:
        raise OperationContractError(
            "ambiguous_model_output", "Out-of-scope responses cannot also contain operations."
        )
    return envelope


CHART_OPERATION_SAFETY_CONTRACT = r"""
CODE-OWNED CHART OPERATION CONTRACT (mandatory; overrides conflicting text above)
Return only one compact JSON object:
{"operations":[...],"notes":"optional short note","out_of_scope":false,"reason_code":null}
Never return chart_config, ECharts option/config, data, rows, SQL, code, or copied prompt text.

Allowed operations (no extra keys):
- {"op":"set_color","target":"...","color":"#RRGGBB"}
- {"op":"set_palette","colors":["#RRGGBB",...]} (1-12 colors, in order; use for
  named themes like "India colors", "pastel", "monochrome blues", or to color
  pie slices / several series differently)
- {"op":"set_style","target":"...","path":"<allowlisted path>","value":<scalar>}
- {"op":"set_toggle","key":"dataLabels|legend|dataZoom","value":true,"target":"optional"}
- {"op":"set_format","kind":"number|currency|percent","compact":true,"symbol":""}
- {"op":"rename_series","target":"...","name":"..."}
- {"op":"hide_series","target":"...","hidden":true}
- {"op":"add_overlay","operator":"moving_avg|cumulative_sum|percent_change|linear_trend|normalize_0_1|log_scale","source_column":"...","params":{},"label":"..."}
- {"op":"remove_overlay","operator":"optional","label":"optional"}
- SQL charts only: {"op":"set_sort","direction":"asc|desc|none"},
  {"op":"set_chart_type","chart_type":"bar|line|area|pie|donut|scatter|horizontal_bar|stacked_bar|stacked_area|combo|heatmap|gauge"},
  {"op":"set_stack","stacked":true},
  {"op":"set_binding","x":"optional","y":"optional","series":"optional"}.
  Chart type and binding changes are rebuilt by the app from the same cached
  result rows (same columns, grouping, and aggregation), so they are in scope.
  Only a different GROUP BY, filter, aggregation, or new column needs a new query.
  "Donut" = pie with a hole; "horizontal bar" flips the axes; "stacked" keeps
  the series and stacks them. Sorting/stacking do not apply to pie, donut, or
  gauge; palettes do not apply to heatmap, gauge, or map charts.
- What-if (SQL charts only). The app draws a dashed "Scenario" copy of the
  series next to the real one; the real data is never changed. Categories must
  be taken verbatim from the manifest axis categories.
  {"op":"scenario_set_point","target":"...","category":"<axis label>","value":<number>,"label":"..."}
  {"op":"scenario_scale","target":"...","percent":<-100..1000> | "factor":<0..20>,"from_category":"optional","label":"..."}
  {"op":"scenario_shift","target":"...","delta":<number>,"from_category":"optional","label":"..."}
  {"op":"scenario_clear","label":"optional"}
- Annotations (any chart kind):
  {"op":"add_reference_line","axis":"y","value":<number> | "stat":"avg|min|max|median","target":"optional","label":"..."}
  {"op":"remove_reference_line","label":"optional"}
  {"op":"highlight_points","target":"...","categories":["..."] | "predicate":{"op":"lt|lte|gt|gte|eq|between","value":<n>,"value2":<n>},"color":"optional #RRGGBB","label":"optional"}
  {"op":"clear_highlights"}
  Parse "30K" as 30000, "1.2M" as 1200000, "10%" growth as percent 10.

Targets are exactly "all", "role:<manifest jeenRole>", "name:<manifest name>", or
"id:<manifest id>". Never use an array index. Do not target locked helper/bound
series. The visible role:interval band may only change safe fill color/opacity.
When the same answer also changes chart type or bindings, color with
set_palette or target "all" (series names may change after the rebuild).
For ml_band and ml_basic, never emit set_chart_type, set_sort, set_stack,
set_palette, set_binding, or scenario_* (a what-if on a model result is a new
analysis). Use only manifest columns, series, and axis categories.
Safe style paths: itemStyle.color/borderColor/opacity/borderWidth,
lineStyle.color/width/opacity/type, areaStyle.color/opacity, opacity,
symbolSize, symbol, smooth, label.show/color/position/fontSize, and
label.formatter. Numeric opacity is 0..1, widths are bounded, line type is
solid/dashed/dotted, and symbol is allowlisted. Formatter values
may be plain text plus {a}, {b}, {c}, {d}, {value}, {seriesName}, or {name};
never functions, HTML, URLs, or JavaScript.
If the request needs new aggregation/grouping, SQL/data access, a new ML
analysis, or an unsupported edit, return no operations, out_of_scope=true, and
a stable snake_case reason_code.

Short examples:
1. "show line chart in green with labels" (SQL):
{"operations":[{"op":"set_chart_type","chart_type":"line"},{"op":"set_color","target":"all","color":"#22c55e"},{"op":"set_toggle","key":"dataLabels","value":true}],"notes":"Line chart, green series, and labels.","out_of_scope":false,"reason_code":null}
2. "make the observed ML line blue":
{"operations":[{"op":"set_color","target":"role:actual","color":"#2563eb"}],"notes":"Updated the actual series.","out_of_scope":false,"reason_code":null}
3. "hide Forecast":
{"operations":[{"op":"hide_series","target":"name:Forecast","hidden":true}],"notes":"Forecast hidden.","out_of_scope":false,"reason_code":null}
4. "show dollars without abbreviation":
{"operations":[{"op":"set_format","kind":"currency","compact":false,"symbol":"$"}],"notes":"Values use full USD amounts.","out_of_scope":false,"reason_code":null}
5. "add a 3 month moving average":
{"operations":[{"op":"add_overlay","operator":"moving_avg","source_column":"revenue","params":{"window":3},"label":"3-month moving average"}],"notes":"Added the overlay.","out_of_scope":false,"reason_code":null}
6. "remove the trend":
{"operations":[{"op":"remove_overlay","operator":"linear_trend"}],"notes":"Removed the trend overlay.","out_of_scope":false,"reason_code":null}
7. Hebrew, "הצג מקרא ותוויות":
{"operations":[{"op":"set_toggle","key":"legend","value":true},{"op":"set_toggle","key":"dataLabels","value":true}],"notes":"המקרא והתוויות מוצגים.","out_of_scope":false,"reason_code":null}
8. "group by quarter instead":
{"operations":[],"notes":"Grouping requires a new query.","out_of_scope":true,"reason_code":"needs_new_query"}
9. "turn this ML result into a pie":
{"operations":[],"notes":"ML result chart structure is fixed.","out_of_scope":true,"reason_code":"ml_locked"}
10. "make the line thicker":
{"operations":[{"op":"set_style","target":"all","path":"lineStyle.width","value":3}],"notes":"Increased line width.","out_of_scope":false,"reason_code":null}
11. "change to pie chart with India colors" (SQL):
{"operations":[{"op":"set_chart_type","chart_type":"pie"},{"op":"set_palette","colors":["#ff9933","#138808","#000080","#ffffff"]}],"notes":"Pie chart in saffron, green, navy, and white.","out_of_scope":false,"reason_code":null}
12. "horizontal bars, biggest first" (SQL):
{"operations":[{"op":"set_chart_type","chart_type":"horizontal_bar"},{"op":"set_sort","direction":"desc"}],"notes":"Horizontal bars sorted descending.","out_of_scope":false,"reason_code":null}
13. "use pastel colors":
{"operations":[{"op":"set_palette","colors":["#a5d8ff","#b2f2bb","#ffd8a8","#ffc9c9","#d0bfff","#fff3bf"]}],"notes":"Applied a pastel palette.","out_of_scope":false,"reason_code":null}
14. "what if Bikes sold 30K" (SQL, category "Bikes" in the manifest):
{"operations":[{"op":"scenario_set_point","target":"all","category":"Bikes","value":30000,"label":"Bikes at 30K"}],"notes":"Added a scenario with Bikes at 30,000 next to the actual values.","out_of_scope":false,"reason_code":null}
15. "show 10% growth from June":
{"operations":[{"op":"scenario_scale","target":"all","percent":10,"from_category":"June","label":"+10% from June"}],"notes":"Scenario: +10% from June onward.","out_of_scope":false,"reason_code":null}
16. "add a target line at 20K and highlight months below 10K":
{"operations":[{"op":"add_reference_line","axis":"y","value":20000,"label":"Target 20K"},{"op":"highlight_points","target":"all","predicate":{"op":"lt","value":10000},"label":"Below 10K"}],"notes":"Target line and highlights added.","out_of_scope":false,"reason_code":null}
17. "show the average line":
{"operations":[{"op":"add_reference_line","axis":"y","stat":"avg","label":"Average"}],"notes":"Average line added.","out_of_scope":false,"reason_code":null}
18. "what if total revenue were 1M":
{"operations":[],"notes":"Say how to spread the total, e.g. 'scale every month so the total is 1M' or 'what if December were 1M'.","out_of_scope":true,"reason_code":"ambiguous"}
19. "remove the scenario":
{"operations":[{"op":"scenario_clear"}],"notes":"Scenario removed.","out_of_scope":false,"reason_code":null}
"""
