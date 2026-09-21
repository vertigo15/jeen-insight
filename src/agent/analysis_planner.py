"""Analysis planner: bind a ``needs_analysis`` question to one registered skill.

Shaped like :mod:`src.agent.tool_planner`: a cheap local pre-filter, then one
temperature-0 JSON call, then deterministic validation against the catalog.
The planner never derives parameters from result *values* — only from the
catalog and the user's words — and never raises into the graph.

Outcomes
--------
``params``    a validated params dict for the chosen skill
``clarify``   several catalog columns fit equally; ask, with the candidates
              as executable options
``fallback``  no skill applies (or the LLM output was unusable); the graph
              answers with SQL instead
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pydantic import ValidationError

from src.analysis.contracts import (
    DEFAULT_WINDOW_PERIODS,
    SKILLS,
    GuardExit,
    method_options,
    parse_params,
)
from src.analysis.sql_builder import filters_from_grounder
from src.metadata.identifiers import split_qualified_identifier

logger = logging.getLogger(__name__)

_NUMERIC_TYPES = ("int", "decimal", "numeric", "float", "double", "real", "money", "number", "bigint", "smallint")
_DATE_TYPES = ("date", "timestamp", "datetime")
_TYPE_RE = re.compile(r"\btype\s*:\s*([^,|]+)", re.IGNORECASE)
_PK_RE = re.compile(r"\bPK\s*:\s*true\b", re.IGNORECASE)
_MAX_OPTIONS = 6

# Cues strong enough to upgrade a router's needs_query on their own. Weak cues
# ("drop", "spike", "expect") only inform the LLM hint.
_STRONG_CUES: Dict[str, Sequence[str]] = {
    "forecast": (
        r"\bfore?cas?t(s|ed|ing)?\b", r"\bpredict(?!s|or)", r"\bprojection", r"\bproject(ed|ion)?\b.*\b(next|coming|future)\b",
        r"\bnext (quarter|month|year|week|\d+ (days|weeks|months))\b", r"\brun[- ]rate\b", r"\bat this rate\b",
        r"\bwhat will\b", r"\bhow much will\b", r"\bwill .* (be|reach|grow|fall|hit)\b",
    ),
    "driver_analysis": (
        r"\bwhat predicts\b", r"\bwhat drives\b", r"\bdrivers? of\b", r"\bwhich factors\b", r"\bfeature importance\b",
        r"\bwhat influences\b", r"\bdeterminants of\b",
    ),
    "regression": (
        r"\blinear regression\b", r"\bregression (model|analysis)\b", r"\bregress\b", r"\bcoefficients?\b",
        r"\belasticit(y|ies)\b", r"\beffect of .+ on\b", r"\bhow much does .+ (affect|impact|influence)\b",
        r"\bsensitivity of\b", r"\bmarginal effect\b",
    ),
    "classification": (
        r"\bchurn", r"\bpropensity\b", r"\blikelihood of\b", r"\bprobability of\b",
        r"\bpredict (who|which|whether|if)\b", r"\bwho (is|are) likely to\b",
        r"\blikely to (churn|convert|buy|leave|default|cancel|renew)\b", r"\brisk of\b", r"\bclassify\b",
    ),
    "contribution": (
        r"\bwhy did .* (drop|fall|rise|grow|change|increase|decrease|decline)", r"\bwhat explains the (drop|change|rise|fall|decline)",
        r"\bwhat drove the\b", r"\bbreakdown of the change\b", r"\bwhich .* account(s)? for\b", r"\bcontribution (to|of) the\b",
    ),
    "changepoint": (
        r"\bwhen did .* (change|shift|start|break)", r"\btrend break\b", r"\bregime\b", r"\bstructural break\b",
        r"\bchange ?points?\b", r"\binflection\b", r"\bturning point\b",
    ),
    "seasonality": (
        r"\bseasonal(ity)?\b", r"\bcyclical\b", r"\bpeak (month|week|season|time)\b", r"\btime of year\b",
    ),
    "correlation": (
        r"\bcorrelat", r"\bmove(s)? (together|with)\b", r"\brelationship between\b", r"\blead(ing)? indicator\b",
        r"\blagged\b", r"\bdoes .* (drive|track|follow) ",
    ),
    "cohort_retention": (
        r"\bretention\b", r"\bretention (curve|rate|analysis)\b", r"\bcohort (analysis|retention|curve)\b",
        r"\bkeep coming back\b", r"\bcome back\b", r"\breturn(ing)? (rate|users?|customers?)\b",
        r"\brepeat (purchase|customers?|rate)\b", r"\bstickiness\b", r"\bchurn (curve|over time)\b",
        r"\bhow many .* (return|come back|stay active|stick around)\b",
    ),
    "experiment_test": (
        r"\ba/?b test", r"\bexperiment\b", r"\bvariant\b", r"\btreatment (group|arm|vs|and control)\b",
        r"\bcontrol (group|arm|vs)\b", r"\bstatistical(ly)? significan", r"\bsignificant (difference|lift|uplift|effect)\b",
        r"\buplift\b", r"\bdid .* (beat|outperform|win)\b", r"\bdifference between .* (groups|arms|variants)\b",
        r"\bhypothesis test\b", r"\btest (whether|if) .* (better|higher|lower|different)\b",
    ),
    "clustering": (
        r"\bsegment(s|ation)?\b", r"\bcluster", r"\bgroup (the |our )?(customers|products|stores|users|accounts)\b",
        r"\btypes of (customers|products|users)\b", r"\bpersonas\b", r"\bcohorts\b",
    ),
    "anomaly_detection": (
        r"\banomal", r"\boutlier", r"\bunusual", r"\bweird", r"\babnormal", r"\birregular",
        r"\bis (this|that|it) normal\b", r"\bout of the ordinary\b", r"\bunexpected (drop|spike|jump|change)s?\b",
        r"\bspikes?\b", r"\bstrange\b",
    ),
}
_STRONG_RE = {skill: re.compile("|".join(pats), re.IGNORECASE) for skill, pats in _STRONG_CUES.items()}
# Most specific cues first; anomaly last because its words are the most generic.
_CUE_ORDER = ("experiment_test", "driver_analysis", "regression", "classification", "contribution", "changepoint",
              "seasonality", "correlation", "cohort_retention", "clustering", "forecast", "anomaly_detection")


def detect_analysis_intent(question: str) -> Optional[str]:
    """Keyword pre-filter. Returns a skill name or ``None``. No LLM."""
    text = (question or "").strip()
    if not text:
        return None
    for skill in _CUE_ORDER:
        if _STRONG_RE[skill].search(text):
            return skill
    return None


# ── Catalog candidates ────────────────────────────────────────────────────────


@dataclass
class TableCandidates:
    name: str  # catalog spelling
    date_columns: List[str] = field(default_factory=list)
    numeric_columns: List[str] = field(default_factory=list)
    text_columns: List[str] = field(default_factory=list)  # dimensions / entity keys
    types: Dict[str, str] = field(default_factory=dict)  # lower column -> type
    schema: Optional[str] = None  # catalog spelling when the metadata qualifies the table
    primary_keys: List[str] = field(default_factory=list)  # columns the catalog flags "PK: true"

    def entity_key_guess(self) -> Optional[str]:
        """The one column that identifies an entity, when the catalog makes it
        unambiguous: a single primary key, else a single ``*key``/``*id`` column."""
        if len(self.primary_keys) == 1:
            return self.primary_keys[0]
        keys = [c for c in self.text_columns + self.numeric_columns if c.lower().endswith(("key", "id"))]
        return keys[0] if len(keys) == 1 else None

    def canonical_column(self, name: Optional[str]) -> Optional[str]:
        """The catalog's exact spelling of ``name`` (case-insensitive lookup), or None."""
        if not name:
            return None
        wanted = str(name).lower()
        for c in self.date_columns + self.numeric_columns + self.text_columns:
            if c.lower() == wanted:
                return c
        return None


def catalog_candidates(columns_text: str) -> Dict[str, TableCandidates]:
    """Parse the metadata ``columns`` block into per-table date/numeric columns.

    Lines look like ``- Table.Column - Type: decimal, ...``. Case is preserved
    because Postgres quoted identifiers are case-sensitive.
    """
    out: Dict[str, TableCandidates] = {}
    for line in (columns_text or "").splitlines():
        stripped = line.lstrip("- ").strip()
        if not stripped:
            continue
        qualified = stripped.split(" - ", 1)[0].strip()
        parts = split_qualified_identifier(qualified)
        if len(parts) < 2:
            continue
        table, column = parts[-2], parts[-1]
        match = _TYPE_RE.search(stripped)
        dtype = match.group(1).strip().lower() if match else ""
        tc = out.setdefault(table.lower(), TableCandidates(name=table))
        if len(parts) >= 3 and not tc.schema:
            tc.schema = parts[-3]
        tc.types[column.lower()] = dtype
        if _PK_RE.search(stripped) and column not in tc.primary_keys:
            tc.primary_keys.append(column)
        if any(t in dtype for t in _DATE_TYPES) and "time" != dtype:
            if column not in tc.date_columns:
                tc.date_columns.append(column)
        elif any(t in dtype for t in _NUMERIC_TYPES):
            if column not in tc.numeric_columns:
                tc.numeric_columns.append(column)
        elif column not in tc.text_columns:
            tc.text_columns.append(column)
    return out


def column_types_map(cands: Dict[str, TableCandidates]) -> Dict[Tuple[str, str], str]:
    return {(t, c): dtype for t, tc in cands.items() for c, dtype in tc.types.items()}


def render_catalog(cands: Dict[str, TableCandidates]) -> str:
    """Tables with a date column host time series; tables with ≥2 numeric
    columns can host entity skills (clustering, drivers) even without dates."""
    lines: List[str] = []
    for tc in sorted(cands.values(), key=lambda t: t.name.lower()):
        if not tc.date_columns and len(tc.numeric_columns) < 2:
            continue
        lines.append(
            f"- {tc.name}: dates [{', '.join(tc.date_columns) or 'none'}]; measures "
            f"[{', '.join(tc.numeric_columns[:25]) or 'none (row counts only)'}]; "
            f"dimensions/keys [{', '.join(tc.text_columns[:20]) or 'none'}]"
        )
    return "\n".join(lines) or "No table with a date column or numeric measures is registered."


def render_skills() -> str:
    return "\n".join(f"- `{s.name}` — {s.description}" for s in SKILLS.values())


# ── Plan outcome ──────────────────────────────────────────────────────────────


@dataclass
class PlanOutcome:
    kind: str  # params | clarify | fallback
    skill: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    message: str = ""
    options: List[GuardExit] = field(default_factory=list)
    dropped_filters: List[str] = field(default_factory=list)
    reason: str = ""
    prompt: str = ""
    usage: Dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    # The raw LLM plan; a clarification is resolved by patching this and
    # re-running build_params_from_plan server-side (see routes/analysis.py).
    plan: Dict[str, Any] = field(default_factory=dict)


def _extract_json(content: str) -> Dict[str, Any]:
    text = (content or "").strip()
    if "```" in text:
        start = text.find("```") + 3
        if text[start:].startswith("json"):
            start += 4
        end = text.find("```", start)
        text = text[start:end].strip() if end > start else text
    start, end = text.find("{"), text.rfind("}") + 1
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start:end])
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _pick(name: Optional[str], options: Sequence[str]) -> Optional[str]:
    if not name:
        return None
    low = str(name).strip().lower()
    for opt in options:
        if opt.lower() == low:
            return opt
    return None


def _option_exits(key: str, candidates: Sequence[str]) -> List[GuardExit]:
    exits = []
    for i, c in enumerate(candidates[:_MAX_OPTIONS]):
        exits.append(GuardExit(
            kind="patch", label=c, description="", params_patch={"series": {key: c}}, recommended=(i == 0),
        ))
    return exits


def _coerce_float(value: Any, lo: float, hi: float) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi else None


def _coerce_int(value: Any, lo: int, hi: int) -> Optional[int]:
    try:
        i = int(value)
    except (TypeError, ValueError):
        return None
    return i if lo <= i <= hi else None


def _coerce_date(value: Any) -> Optional[str]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


# Spoken names the planner (or a user quoting a model) may use for a method,
# mapped to the contract enum. The exact enum value always wins; these only
# rescue a near-miss so "3 sigma" / "ETS" / "seasonal naive" still land.
_METHOD_ALIASES: Dict[str, str] = {
    "3-sigma": "sigma3", "3 sigma": "sigma3", "3sigma": "sigma3", "sigma": "sigma3", "sigma3": "sigma3",
    "ets": "auto_ets", "arima": "auto_arima",
    "seasonal-naive": "seasonal_naive", "seasonal naive": "seasonal_naive", "naive": "seasonal_naive",
}


def _method(plan: Dict[str, Any], skill: str) -> Optional[str]:
    """A model override from the plan, only when it is a valid method of ``skill``.

    Left unset (the card's default of ``auto``) when absent or unrecognised, so
    a planner that omits or hallucinates a method never breaks the run.
    """
    raw = plan.get("method")
    if not raw:
        return None
    key = str(raw).strip().lower()
    options = method_options(skill)
    if key in options:
        return key
    alias = _METHOD_ALIASES.get(key)
    return alias if alias in options else None


# Calendar-part column names. A forecast must not scope its *history* by the
# period it is asked to project: the grounder turns "forecast for Jul-Dec 2008"
# into year/month filters, which — applied to the training data — would empty
# the series. Matched as whole, separator-normalised names (NOT substrings) so
# "CandidateStatus"/"YearlyIncome" are never mistaken for calendar columns.
_TEMPORAL_COLUMNS = frozenset({
    "year", "calendaryear", "fiscalyear", "yearmonth",
    "quarter", "calendarquarter", "fiscalquarter",
    "month", "monthname", "monthnumber", "calendarmonth", "monthofyear",
    "week", "weeknumber", "weekofyear", "calendarweek",
    "day", "dayofweek", "dayofmonth", "dayofyear",
    "date", "calendardate", "period", "fiscalperiod", "reportingperiod",
})


def _norm_col(column: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (column or "").lower())


def _is_temporal_column(column: Optional[str], date_column: Optional[str]) -> bool:
    c = _norm_col(column)
    if not c:
        return False
    return c == _norm_col(date_column) or c in _TEMPORAL_COLUMNS


def _is_calendar_table(table: Optional[str]) -> bool:
    """A date/calendar dimension: every filter on it names a period, not an entity."""
    name = _norm_col(table)
    return bool(name) and any(token in name for token in ("date", "calendar", "period", "dimtime"))


def _is_calendar_range_filter(item: Dict[str, Any], cands: Dict[str, "TableCandidates"]) -> bool:
    """A grounded ``between`` on a date column of a calendar table (DimDate).

    Only a calendar table qualifies: a range on DimCustomer.SignupDate is a
    filter on who is counted, not the period to project.
    """
    if str(item.get("op") or "").lower() != "between":
        return False
    value = item.get("value")
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return False
    table = str(item.get("table") or "").strip()
    if not _is_calendar_table(table):
        return False
    tc = cands.get(table.lower())
    column = str(item.get("column") or "").strip().lower()
    return bool(tc) and column in {c.lower() for c in tc.date_columns}


def _closed_range_periods(start: Optional[str], last: Optional[str], grain: str) -> Optional[int]:
    """:func:`_range_periods` for a closed ``[start, last]`` range, as the grounder emits."""
    last_day = _coerce_date(last)
    if last_day is None:
        return None
    end_excl = (date.fromisoformat(last_day) + timedelta(days=1)).isoformat()
    return _range_periods(_coerce_date(start), end_excl, grain)


def _range_periods(start: Optional[str], end: Optional[str], grain: str) -> Optional[int]:
    """How many ``grain`` periods a [start, end) range covers, or None.

    Turns a named forecast target window into a horizon. ``end`` is treated as
    exclusive (the planner's convention); stepping back one day makes an
    inclusive end (2008-12-31) and an exclusive one (2009-01-01) count the same
    number of grain buckets — so "Jul-Dec 2008" is 6 months either way."""
    if not start or not end:
        return None
    try:
        s = date.fromisoformat(str(start)[:10])
        e = date.fromisoformat(str(end)[:10])
    except ValueError:
        return None
    if e <= s:
        return None
    last = max(s, e - timedelta(days=1))
    if grain == "month":
        n = (last.year - s.year) * 12 + (last.month - s.month) + 1
    elif grain == "week":
        n = (last - s).days // 7 + 1
    else:
        n = (last - s).days + 1
    return max(1, min(int(n), 104))


def build_params_from_plan(
    plan: Dict[str, Any],
    cands: Dict[str, TableCandidates],
    *,
    resolved_filters: Sequence[Dict[str, Any]],
    connection_schema: Optional[str],
    connection_catalog: Optional[str],
) -> PlanOutcome:
    """Deterministic validation of the LLM plan against the catalog."""
    skill = str(plan.get("skill") or "none").strip().lower()
    if skill not in SKILLS:
        return PlanOutcome(kind="fallback", reason=str(plan.get("reason") or "no skill applies"))
    spec = SKILLS[skill]

    table_key = str(plan.get("table") or "").strip().lower()
    tc = cands.get(table_key)
    ambiguous = plan.get("ambiguous") if isinstance(plan.get("ambiguous"), dict) else {}

    if spec.family == "entity":
        return _build_entity_params(skill, plan, tc, cands, resolved_filters=resolved_filters,
                                    connection_schema=connection_schema, connection_catalog=connection_catalog)

    if spec.family == "cohort":
        return _build_cohort_params(skill, plan, tc, cands, resolved_filters=resolved_filters,
                                    connection_schema=connection_schema, connection_catalog=connection_catalog)

    if spec.family == "experiment":
        return _build_experiment_params(skill, plan, tc, cands, resolved_filters=resolved_filters,
                                        connection_schema=connection_schema, connection_catalog=connection_catalog)

    if tc is None:
        tables_with_dates = [t.name for t in cands.values() if t.date_columns]
        amb_tables = [t for t in (ambiguous.get("table") or []) if _pick(t, tables_with_dates)]
        if len(amb_tables) >= 2:
            return PlanOutcome(
                kind="clarify", skill=skill,
                message="Which table should I analyse?",
                options=[GuardExit(kind="patch", label=_pick(t, tables_with_dates) or t, params_patch={"series": {"table": _pick(t, tables_with_dates)}}, recommended=(i == 0)) for i, t in enumerate(amb_tables[:_MAX_OPTIONS])],
                reason="table ambiguous",
            )
        if len(tables_with_dates) == 1:
            tc = cands[tables_with_dates[0].lower()]
        else:
            return PlanOutcome(kind="fallback", reason=f"table {plan.get('table')!r} is not in the catalog")

    # Date column: the plan's pick, else the only candidate, else clarify.
    date_col = _pick(plan.get("date_column"), tc.date_columns)
    amb_dates = [c for c in (ambiguous.get("date_column") or []) if _pick(c, tc.date_columns)]
    if len(amb_dates) >= 2:
        return PlanOutcome(
            kind="clarify", skill=skill,
            message=f"Which date should I use on {tc.name}?",
            options=_option_exits("date_column", [_pick(c, tc.date_columns) for c in amb_dates]),
            reason="date column ambiguous",
        )
    if date_col is None:
        if len(tc.date_columns) == 1:
            date_col = tc.date_columns[0]
        elif len(tc.date_columns) > 1:
            return PlanOutcome(
                kind="clarify", skill=skill,
                message=f"Which date should I use on {tc.name}?",
                options=_option_exits("date_column", tc.date_columns),
                reason="no date column chosen",
            )
        else:
            return PlanOutcome(kind="fallback", reason=f"{tc.name} has no date column")

    # Measure: numeric column, or '*' with count.
    agg = str(plan.get("agg") or "sum").strip().lower()
    raw_measure = str(plan.get("measure_column") or "").strip()
    measure: Optional[str]
    if raw_measure == "*" or (agg == "count" and not raw_measure):
        measure, agg = "*", "count"
    else:
        measure = _pick(raw_measure, tc.numeric_columns)
        amb_measures = [c for c in (ambiguous.get("measure_column") or []) if _pick(c, tc.numeric_columns)]
        if len(amb_measures) >= 2:
            return PlanOutcome(
                kind="clarify", skill=skill,
                message=f"Which measure should I analyse on {tc.name}?",
                options=_option_exits("measure_column", [_pick(c, tc.numeric_columns) for c in amb_measures]),
                reason="measure ambiguous",
            )
        if measure is None:
            if len(tc.numeric_columns) == 1:
                measure = tc.numeric_columns[0]
            elif tc.numeric_columns:
                return PlanOutcome(
                    kind="clarify", skill=skill,
                    message=f"Which measure should I analyse on {tc.name}?",
                    options=_option_exits("measure_column", tc.numeric_columns),
                    reason="no measure chosen",
                )
            else:
                measure, agg = "*", "count"
    if agg not in ("sum", "count", "avg", "min", "max"):
        agg = "sum"

    grain = str(plan.get("grain") or "week").strip().lower()
    if grain not in ("day", "week", "month"):
        grain = "week"

    filters, dropped = filters_from_grounder(resolved_filters, tc.name)
    # A grounded range on the series' own date column is the analysis window.
    start = _coerce_date(plan.get("start"))
    end = _coerce_date(plan.get("end"))
    kept_filters = []
    for f in filters:
        if f.column.lower() == date_col.lower() and f.op == "between" and isinstance(f.value, (list, tuple)) and len(f.value) == 2:
            start = start or _coerce_date(f.value[0])
            end = end or _coerce_date(f.value[1])
            continue
        kept_filters.append(f)

    group_by = _pick(plan.get("group_by"), (tc.text_columns + tc.numeric_columns)) if plan.get("group_by") else None
    if group_by and group_by.lower() in (date_col.lower(), measure.lower()):
        group_by = None
    series: Dict[str, Any] = {
        "table": tc.name,
        "schema_name": connection_schema or None,
        "catalog": connection_catalog or None,
        "date_column": date_col,
        "measure_column": measure,
        "agg": agg,
        "grain": grain,
        "start": start,
        "end": end,
        "filters": [f.model_dump() for f in kept_filters],
        "group_by": group_by,
    }
    params: Dict[str, Any] = {"series": series}
    window = _coerce_int(plan.get("window_periods"), 12, 1500)
    if skill == "anomaly_detection":
        if window:
            params["window"] = window
        sens = _coerce_float(plan.get("sensitivity"), 0.80, 0.99)
        if sens is not None:
            params["sensitivity"] = round(sens, 3)
        method = _method(plan, skill)
        if method:
            params["method"] = method
    elif skill == "forecast":
        horizon = _coerce_int(plan.get("horizon"), 1, 104)
        # A forecast projects FROM history INTO the future. A named target
        # period ("forecast for Jul-Dec 2008") — whether the planner expressed
        # it as start/end or the grounder derived year/month filters from it —
        # is the horizon to PROJECT, not a filter on the training history:
        # applying it would leave an empty series (the guard's span probe then
        # blocks with "0 rows match the requested filters"). So derive the
        # horizon from that window, forecast from all available history, and
        # drop the temporal filters. A genuine training window is uncommon; the
        # confirm card lets the user set start/end explicitly.
        span = _range_periods(start, end, grain)
        if span:
            horizon = span
        elif horizon is None:
            # Neither a horizon nor a window in the plan: the grounder may have
            # bound the period to the calendar table (DimDate.FullDateAlternateKey);
            # that closed range is the horizon. An explicit plan value wins over it.
            for item in resolved_filters or []:
                if _is_calendar_range_filter(item, cands):
                    horizon = _closed_range_periods(item["value"][0], item["value"][1], grain)
                    if horizon:
                        break
        series["start"] = None
        series["end"] = None
        series["filters"] = [
            f.model_dump() for f in kept_filters
            if not _is_temporal_column(f.column, date_col)
        ]
        # A calendar-table range was the target period, not a lost filter: do not
        # report it as "not applied". Every other cross-table filter still is.
        temporal_dropped = {
            f"{str(item.get('table') or '').strip()}.{str(item.get('column') or '').strip()}"
            for item in (resolved_filters or [])
            if _is_calendar_range_filter(item, cands)
        }
        dropped = [d for d in dropped if d not in temporal_dropped]
        if horizon:
            params["horizon"] = horizon
        interval = _coerce_float(plan.get("interval"), 0.50, 0.99)
        if interval is not None:
            params["interval"] = round(interval, 2)
        if window:
            params["window"] = window  # the guard fills the history range from it
        method = _method(plan, skill)
        if method:
            params["method"] = method
    elif skill in ("changepoint", "seasonality"):
        if window:
            params["window"] = max(window, 24) if skill == "seasonality" else window
    elif skill == "correlation":
        other = _pick(plan.get("other_measure_column"), tc.numeric_columns)
        others = [c for c in tc.numeric_columns if c.lower() != measure.lower()]
        if other is None or other.lower() == measure.lower():
            if len(others) == 1:
                other = others[0]
            elif others:
                return PlanOutcome(kind="clarify", skill=skill,
                                   message=f"Which second measure should I correlate {measure} with?",
                                   options=[GuardExit(kind="patch", label=c, params_patch={"other_measure_column": c}, recommended=(i == 0))
                                            for i, c in enumerate(others[:_MAX_OPTIONS])],
                                   reason="second measure ambiguous")
            else:
                return PlanOutcome(kind="fallback", reason=f"{tc.name} has only one numeric measure")
        params["other_measure_column"] = other
        other_agg = str(plan.get("other_agg") or "sum").strip().lower()
        params["other_agg"] = other_agg if other_agg in ("sum", "count", "avg", "min", "max") else "sum"
        max_lag = _coerce_int(plan.get("max_lag"), 0, 52)
        if max_lag is not None:
            params["max_lag"] = max_lag
        if window:
            params["window"] = window
    elif skill == "contribution":
        raw_dims = plan.get("dimensions") if isinstance(plan.get("dimensions"), list) else []
        dims = [d for d in (_pick(x, tc.text_columns + tc.numeric_columns) for x in raw_dims) if d and d.lower() not in (date_col.lower(), measure.lower())]
        dims = list(dict.fromkeys(dims))[:4]
        if not dims:
            if len(tc.text_columns) == 1:
                dims = [tc.text_columns[0]]
            elif tc.text_columns:
                return PlanOutcome(kind="clarify", skill=skill,
                                   message=f"Which dimension should I break the change in {measure} down by?",
                                   options=[GuardExit(kind="patch", label=c, params_patch={"dimensions": [c]}, recommended=(i == 0))
                                            for i, c in enumerate(tc.text_columns[:_MAX_OPTIONS])],
                                   reason="dimension ambiguous")
            else:
                return PlanOutcome(kind="fallback", reason=f"{tc.name} has no dimension column to slice by")
        params["dimensions"] = dims
        for key in ("before_start", "before_end", "after_start", "after_end"):
            value = _coerce_date(plan.get(key))
            if value:
                params[key] = value
        top_n = _coerce_int(plan.get("top_n"), 3, 50)
        if top_n:
            params["top_n"] = top_n
        series.pop("group_by", None)
        series["grain"] = "month"

    try:
        typed = parse_params(skill, {k: v for k, v in params.items() if not k.startswith("_")})
    except ValidationError as exc:
        return PlanOutcome(kind="fallback", reason=f"plan failed validation: {exc.errors()[0].get('msg') if exc.errors() else exc}")
    out = typed.model_dump(mode="json")
    return PlanOutcome(
        kind="params", skill=skill, params=out, dropped_filters=dropped,
        reason=str(plan.get("reason") or ""),
    )


def _build_entity_params(skill, plan, tc, cands, *, resolved_filters, connection_schema, connection_catalog) -> PlanOutcome:
    """Tier B: entity key + numeric features (+ target) on one table."""
    if tc is None:
        candidates = [t.name for t in cands.values() if len(t.numeric_columns) >= 2]
        if len(candidates) == 1:
            tc = cands[candidates[0].lower()]
        elif len(candidates) > 1:
            return PlanOutcome(kind="clarify", skill=skill, message="Which table holds the entities?",
                               options=[GuardExit(kind="patch", label=c, params_patch={"entity": {"table": c}}, recommended=(i == 0))
                                        for i, c in enumerate(candidates[:_MAX_OPTIONS])], reason="table ambiguous")
        else:
            return PlanOutcome(kind="fallback", reason="no table with two or more numeric columns")
    key = _pick(plan.get("entity_key"), tc.text_columns + tc.numeric_columns)
    if key is None:
        # A sole primary key settles it (the catalog flags it); otherwise a sole
        # *key/*id column; several candidates are the user's call.
        key = tc.entity_key_guess()
    if key is None:
        keys = [c for c in tc.text_columns + tc.numeric_columns if c.lower().endswith(("key", "id"))]
        if keys:
            return PlanOutcome(kind="clarify", skill=skill, message=f"Which column identifies one entity in {tc.name}?",
                               options=[GuardExit(kind="patch", label=c, params_patch={"entity": {"entity_key": c}}, recommended=(i == 0)) for i, c in enumerate(keys[:_MAX_OPTIONS])],
                               reason="entity key ambiguous")
        return PlanOutcome(kind="fallback", reason=f"{tc.name} has no identifying column")
    raw_features = plan.get("features") if isinstance(plan.get("features"), list) else []
    features = [f for f in (_pick(x, tc.numeric_columns) for x in raw_features) if f and f.lower() != key.lower()]
    needs_target = skill in ("driver_analysis", "regression", "classification")
    target = _pick(plan.get("target"), tc.numeric_columns) if needs_target else None
    features = [f for f in dict.fromkeys(features) if not target or f.lower() != target.lower()][:8]
    if len(features) < 2:
        auto = [c for c in tc.numeric_columns if c.lower() != key.lower() and (not target or c.lower() != target.lower())][:6]
        if len(auto) >= 2:
            features = auto
        else:
            return PlanOutcome(kind="fallback", reason=f"{tc.name} has fewer than two numeric features")
    if needs_target and target is None:
        options = [c for c in tc.numeric_columns if c.lower() != key.lower()]
        verb = {"regression": "the regression model", "classification": "the model predict"}.get(skill, "the drivers explain")
        return PlanOutcome(kind="clarify", skill=skill, message=f"Which measure should {verb} on {tc.name}?",
                           options=[GuardExit(kind="patch", label=c, params_patch={"entity": {"target": c}}, recommended=(i == 0))
                                    for i, c in enumerate(options[:_MAX_OPTIONS])], reason="target ambiguous")
    filters, dropped = filters_from_grounder(resolved_filters, tc.name)
    entity: Dict[str, Any] = {
        "table": tc.name, "schema_name": connection_schema or None, "catalog": connection_catalog or None,
        "entity_key": key, "features": features, "target": target,
        "filters": [f.model_dump() for f in filters],
    }
    row_cap = _coerce_int(plan.get("row_cap"), 100, 200_000)
    if row_cap:
        entity["row_cap"] = row_cap
    params: Dict[str, Any] = {"entity": entity}
    if skill == "clustering":
        k = _coerce_int(plan.get("k"), 2, 8)
        if k:
            params["k"] = k
    try:
        typed = parse_params(skill, params)
    except ValidationError as exc:
        return PlanOutcome(kind="fallback", reason=f"plan failed validation: {exc.errors()[0].get('msg') if exc.errors() else exc}")
    return PlanOutcome(kind="params", skill=skill, params=typed.model_dump(mode="json"), dropped_filters=dropped,
                       reason=str(plan.get("reason") or ""))


def _build_cohort_params(skill, plan, tc, cands, *, resolved_filters, connection_schema, connection_catalog) -> PlanOutcome:
    """Tier A cohort retention: an entity key and two date columns (signup +
    activity) on one table. Needs a table with at least two date columns."""
    if tc is None:
        candidates = [t.name for t in cands.values() if len(t.date_columns) >= 1]
        two_dates = [t.name for t in cands.values() if len(t.date_columns) >= 2]
        pool = two_dates or candidates
        if len(pool) == 1:
            tc = cands[pool[0].lower()]
        elif len(pool) > 1:
            return PlanOutcome(kind="clarify", skill=skill, message="Which table holds the signups and activity?",
                               options=[GuardExit(kind="patch", label=c, params_patch={"cohort": {"table": c}}, recommended=(i == 0))
                                        for i, c in enumerate(pool[:_MAX_OPTIONS])], reason="table ambiguous")
        else:
            return PlanOutcome(kind="fallback", reason="no table with a date column for cohorts")

    key = _pick(plan.get("entity_key"), tc.text_columns + tc.numeric_columns)
    if key is None:
        key = tc.entity_key_guess()
    if key is None:
        keys = [c for c in tc.text_columns + tc.numeric_columns if c.lower().endswith(("key", "id"))]
        if keys:
            return PlanOutcome(kind="clarify", skill=skill, message=f"Which column identifies one entity in {tc.name}?",
                               options=[GuardExit(kind="patch", label=c, params_patch={"cohort": {"entity_key": c}}, recommended=(i == 0))
                                        for i, c in enumerate(keys[:_MAX_OPTIONS])], reason="entity key ambiguous")
        return PlanOutcome(kind="fallback", reason=f"{tc.name} has no identifying column")

    cohort_date = _pick(plan.get("cohort_date"), tc.date_columns)
    activity_date = _pick(plan.get("activity_date"), tc.date_columns)
    # Heuristic defaults when the plan is silent: a signup/created/first date is
    # the cohort axis; the other date is the activity axis.
    def _match(names):
        for c in tc.date_columns:
            if any(w in c.lower() for w in names):
                return c
        return None
    if cohort_date is None:
        cohort_date = _match(("signup", "sign_up", "created", "registration", "register", "first", "join", "start"))
    if activity_date is None:
        activity_date = _match(("activity", "active", "order", "event", "login", "transaction", "purchase", "last", "seen"))
    if cohort_date is None and len(tc.date_columns) >= 1:
        cohort_date = tc.date_columns[0]
    if activity_date is None:
        activity_date = next((c for c in tc.date_columns if c.lower() != (cohort_date or "").lower()), cohort_date)
    if not cohort_date or not activity_date:
        return PlanOutcome(kind="fallback", reason=f"{tc.name} has no usable signup/activity dates")
    if cohort_date.lower() == activity_date.lower() and len(tc.date_columns) >= 2:
        others = [c for c in tc.date_columns if c.lower() != cohort_date.lower()]
        return PlanOutcome(kind="clarify", skill=skill,
                           message=f"Which date marks activity (as opposed to signup {cohort_date}) on {tc.name}?",
                           options=[GuardExit(kind="patch", label=c, params_patch={"cohort": {"activity_date": c}}, recommended=(i == 0))
                                    for i, c in enumerate(others[:_MAX_OPTIONS])], reason="activity date ambiguous")

    filters, dropped = filters_from_grounder(resolved_filters, tc.name)
    grain = str(plan.get("grain") or "month").strip().lower()
    if grain not in ("day", "week", "month"):
        grain = "month"
    cohort: Dict[str, Any] = {
        "table": tc.name, "schema_name": connection_schema or None, "catalog": connection_catalog or None,
        "entity_key": key, "cohort_date": cohort_date, "activity_date": activity_date, "grain": grain,
        "filters": [f.model_dump() for f in filters],
    }
    max_periods = _coerce_int(plan.get("max_periods"), 2, 36)
    if max_periods:
        cohort["max_periods"] = max_periods
    try:
        typed = parse_params(skill, {"cohort": cohort})
    except ValidationError as exc:
        return PlanOutcome(kind="fallback", reason=f"plan failed validation: {exc.errors()[0].get('msg') if exc.errors() else exc}")
    return PlanOutcome(kind="params", skill=skill, params=typed.model_dump(mode="json"), dropped_filters=dropped,
                       reason=str(plan.get("reason") or ""))


def _build_experiment_params(skill, plan, tc, cands, *, resolved_filters, connection_schema, connection_catalog) -> PlanOutcome:
    """Tier A A/B test: an arm column (categorical) and a numeric outcome on one
    table. Needs a table with at least one categorical and one numeric column."""
    if tc is None:
        candidates = [t.name for t in cands.values() if t.text_columns and t.numeric_columns]
        if len(candidates) == 1:
            tc = cands[candidates[0].lower()]
        elif len(candidates) > 1:
            return PlanOutcome(kind="clarify", skill=skill, message="Which table holds the experiment?",
                               options=[GuardExit(kind="patch", label=c, params_patch={"experiment": {"table": c}}, recommended=(i == 0))
                                        for i, c in enumerate(candidates[:_MAX_OPTIONS])], reason="table ambiguous")
        else:
            return PlanOutcome(kind="fallback", reason="no table with an arm column and a numeric outcome")

    group_column = _pick(plan.get("group_column"), tc.text_columns + tc.numeric_columns)
    if group_column is None:
        arms = [c for c in tc.text_columns if any(w in c.lower() for w in ("variant", "arm", "group", "bucket", "test", "experiment", "cohort", "segment"))]
        if len(arms) == 1:
            group_column = arms[0]
        elif tc.text_columns:
            options = arms or tc.text_columns
            return PlanOutcome(kind="clarify", skill=skill, message=f"Which column splits {tc.name} into arms?",
                               options=[GuardExit(kind="patch", label=c, params_patch={"experiment": {"group_column": c}}, recommended=(i == 0))
                                        for i, c in enumerate(options[:_MAX_OPTIONS])], reason="arm column ambiguous")
        else:
            return PlanOutcome(kind="fallback", reason=f"{tc.name} has no categorical column to split into arms")

    outcome_column = _pick(plan.get("outcome_column"), tc.numeric_columns)
    outcome_type = str(plan.get("outcome_type") or "").strip().lower()
    if outcome_type not in ("binary", "continuous"):
        outcome_type = ""
    if outcome_column is None:
        numeric = [c for c in tc.numeric_columns if c.lower() != group_column.lower()]
        if len(numeric) == 1:
            outcome_column = numeric[0]
        elif numeric:
            return PlanOutcome(kind="clarify", skill=skill, message=f"Which outcome should I test on {tc.name}?",
                               options=[GuardExit(kind="patch", label=c, params_patch={"experiment": {"outcome_column": c}}, recommended=(i == 0))
                                        for i, c in enumerate(numeric[:_MAX_OPTIONS])], reason="outcome ambiguous")
        else:
            return PlanOutcome(kind="fallback", reason=f"{tc.name} has no numeric outcome column")
    if not outcome_type:
        # A 0/1-looking name is treated as a conversion; otherwise a numeric metric.
        outcome_type = "binary" if any(w in outcome_column.lower() for w in
                                       ("convert", "converted", "conversion", "is_", "flag", "success", "clicked", "signup", "purchased", "churn")) else "continuous"

    filters, dropped = filters_from_grounder(resolved_filters, tc.name)
    experiment: Dict[str, Any] = {
        "table": tc.name, "schema_name": connection_schema or None, "catalog": connection_catalog or None,
        "group_column": group_column, "outcome_column": outcome_column, "outcome_type": outcome_type,
        "control": (str(plan.get("control")).strip() if plan.get("control") else None),
        "filters": [f.model_dump() for f in filters],
    }
    params: Dict[str, Any] = {"experiment": experiment}
    conf = _coerce_float(plan.get("confidence"), 0.80, 0.99)
    if conf is not None:
        params["confidence"] = round(conf, 3)
    try:
        typed = parse_params(skill, params)
    except ValidationError as exc:
        return PlanOutcome(kind="fallback", reason=f"plan failed validation: {exc.errors()[0].get('msg') if exc.errors() else exc}")
    return PlanOutcome(kind="params", skill=skill, params=typed.model_dump(mode="json"), dropped_filters=dropped,
                       reason=str(plan.get("reason") or ""))


async def plan_analysis(
    *,
    question: str,
    columns_text: str,
    resolved_filters: Sequence[Dict[str, Any]],
    llm: Any,
    prompt_loader: Any,
    connection_schema: Optional[str] = None,
    connection_catalog: Optional[str] = None,
    skill_hint: Optional[str] = None,
    timeout: Optional[int] = None,
    today: Optional[date] = None,
) -> PlanOutcome:
    """One LLM call + deterministic validation. Never raises."""
    cands = catalog_candidates(columns_text)
    if not any(tc.date_columns or len(tc.numeric_columns) >= 2 for tc in cands.values()):
        return PlanOutcome(kind="fallback", reason="no table with a date column or numeric features is registered")

    today = today or datetime.now(timezone.utc).date()
    prompt = await prompt_loader.arender(
        "analysis_planner",
        question=question,
        skills=render_skills(),
        catalog=render_catalog(cands),
        filters=json.dumps(list(resolved_filters or []), ensure_ascii=False, default=str) or "[]",
        today=today.isoformat(),
        skill_hint=skill_hint or "none",
    )
    model_override = await prompt_loader.model_override_for("analysis_planner")

    t0 = time.monotonic()
    try:
        response = await llm.generate(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": question},
            ],
            temperature=0.0,
            max_tokens=400,
            model_override=model_override,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("analysis_planner: LLM call failed: %s", exc)
        return PlanOutcome(kind="fallback", reason=f"planner unavailable: {type(exc).__name__}", prompt=prompt)
    latency_ms = int((time.monotonic() - t0) * 1000)

    plan = _extract_json(response.get("content") or "")
    if not plan:
        return PlanOutcome(kind="fallback", reason="planner returned no JSON", prompt=prompt,
                           usage=response.get("usage") or {}, latency_ms=latency_ms)
    outcome = build_params_from_plan(
        plan, cands, resolved_filters=resolved_filters,
        connection_schema=connection_schema, connection_catalog=connection_catalog,
    )
    outcome.prompt = prompt
    outcome.usage = response.get("usage") or {}
    outcome.latency_ms = latency_ms
    outcome.plan = plan
    return outcome


# Clarification patches arrive in params_patch shape ({"series": {...}} etc.);
# map them back onto the flat LLM plan before re-validating.
_PLAN_PATCH_KEYS = {
    "table", "date_column", "measure_column", "agg", "grain", "group_by", "other_measure_column", "other_agg",
    "max_lag", "dimensions", "before_start", "before_end", "after_start", "after_end", "top_n", "horizon",
    "sensitivity", "interval", "window_periods", "window", "entity_key", "features", "target", "k",
}


def apply_clarification(plan: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Return the plan with the user's pick applied and the ambiguity cleared."""
    out = dict(plan or {})
    flat: Dict[str, Any] = {}
    for key, value in (patch or {}).items():
        if key in ("series", "entity") and isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    for key, value in flat.items():
        if key == "window":
            out["window_periods"] = value
        elif key in _PLAN_PATCH_KEYS:
            out[key] = value
    out["ambiguous"] = None
    return out


def default_window_periods(grain: str) -> int:
    return DEFAULT_WINDOW_PERIODS.get(grain, 26)


def diff_params(base: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Flat ``{key: {"from": a, "to": b}}`` for the re-run chip row."""
    out: Dict[str, Any] = {}
    for key in sorted(set(base) | set(new)):
        if key in ("series", "entity"):
            for skey in sorted(set(base.get(key) or {}) | set(new.get(key) or {})):
                a = (base.get(key) or {}).get(skey)
                b = (new.get(key) or {}).get(skey)
                if a != b:
                    out[skey] = {"from": a, "to": b}
            continue
        if base.get(key) != new.get(key):
            out[key] = {"from": base.get(key), "to": new.get(key)}
    return out
