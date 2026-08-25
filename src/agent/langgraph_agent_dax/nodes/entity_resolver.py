"""dax_entity_resolver — grounds the plan's filter literals in real model values.

The planner decides *which column* a filter targets; nothing downstream ever
checks that the *literal* it invented exists. Ask "Sales for Mountaiin 300" and
the pipeline happily filters on a misspelling: Power BI returns zero rows with
HTTP 200, so there is no error to classify, the one empty-result diagnostic is
spent regenerating the same wrong literal, and the user is told there is no data
for a product that sells fine.

This node closes that gap between planning and prompt building, which is the
only place both facts are available: the planner has already named the target
column, so a single column is probed instead of searching the whole model, and
the generator has not yet written DAX, so the plan can still be corrected
cheaply. It uses no LLM — resolution is deterministic and therefore testable.

Per literal, in order:

  1. Exact (case/punctuation-insensitive) match -> use the canonical spelling.
  2. Fuzzy matches that contain every token of the user's phrase -> either one
     value (``equals``) or a refinement set such as the Mountain-300 sizes
     (``in``), always recorded as a stated assumption.
  3. Fuzzy matches that are competing alternatives -> ask the user.
  4. Nothing in the target column -> retry across sibling text columns, since
     "Mountain 300" is a product *model*, not a product name.
  5. Still nothing -> tell the user the value does not exist and offer the
     closest ones, which beats an empty table.

Widening a filter to ``IN`` is the one irreversible-looking decision here, so it
is deliberately hard to reach: it needs a multi-token phrase, a *complete* view
of the column, and every candidate must contain every token of that phrase.
Anything less asks the user. The failure it guards against is silent — summing
values nobody asked for still returns a plausible number.

Failure is otherwise always open: a probe that errors, times out, or hits a
column too large to read leaves the plan untouched and records the filter as
unverified, so entity resolution can never be the reason a working query stops
working. Those unverified filters are what lets ``dax_feedback_router`` retry
here on an empty result instead of blindly regenerating.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from src.agent.langgraph_agent_dax.nodes.catalog import _extract_type
from src.agent.langgraph_agent.nodes.filtering import normalize_typed_filter
from src.agent.langgraph_agent_dax.nodes.dax_validate import (
    build_dax_dlp_regex,
    is_governed_name,
)
from src.agent.langgraph_agent_dax.state import DaxAgentState
from src.config import settings
from src.connectors.powerbi import PowerBiDaxClient
from src.connectors.powerbi_probe import PowerBiValueProbe, ProbeResult
from src.connectors.powerbi_token import (
    PowerBiTokenError,
    TokenProviderFactory,
    no_token_provider,
)
from src.metadata.value_index import (
    ValueDomain,
    exact_value,
    is_refinement_set,
    match_values,
    search_tokens,
    tokenize,
    value_domain_cache,
)

logger = logging.getLogger(__name__)

# Filter operators whose value is a literal worth grounding. Range operators are
# excluded: their operands are numbers or dates, not names.
_RESOLVABLE_OPS = {"equals", "in", "contains", "="}

# Column data types whose values are free text. Anything else (numbers, dates,
# booleans) is left alone — a wrong number is not a spelling problem.
_TEXT_TYPES = {
    "string", "text", "varchar", "nvarchar", "char", "nchar", "character varying",
    "str", "unicode", "utf8", "object",
}

# Most questions carry zero or one literal filter; the cap bounds a pathological
# plan rather than normal traffic.
_MAX_FILTERS = 4
# Above this, an IN list stops being a clarification and becomes a shrug.
_MAX_IN_VALUES = 25
# Literals accepted in one `in` filter before it is left alone.
_MAX_NEEDLES = 10
# How many alternatives to name when asking the user.
_MAX_SUGGESTIONS = 5
# Sibling columns probed when the target column yields nothing.
_MAX_CROSS_COLUMNS = 4
# Rows a cross-column CONTAINSSTRING probe may return.
_CROSS_COLUMN_ROWS = 40
# Threshold for the "nothing matched, here is what is closest" suggestion list.
# Looser than the match threshold because these are only ever shown, never used.
_SUGGESTION_THRESHOLD = 55.0

_TARGET_RE = re.compile(r"^\s*'?([^'\[\]]+?)'?\s*\[\s*([^\]]+?)\s*\]\s*$")
# Column names that plausibly hold the kind of value a user names out loud.
_NAMEY_HINTS = ("name", "model", "product", "category", "title", "label", "description", "code")


# A probe is per-request: it is bound to the dataset and the delegated token of
# whoever asked the question, so it cannot be built until the state is in hand.
ProbeFactory = Callable[[DaxAgentState], Optional["PowerBiValueProbe"]]
McpSearch = Callable[[str, str, str], Awaitable[Sequence[str]]]


# ── Catalog helpers ───────────────────────────────────────────────────────────


def parse_target(target: object) -> Optional[Tuple[str, str]]:
    """Split ``'Product'[Product Name]`` into ``("Product", "Product Name")``."""
    match = _TARGET_RE.match(str(target or ""))
    if not match:
        return None
    table, column = match.group(1).strip(), match.group(2).strip()
    return (table, column) if table and column else None


def column_types(columns_text: str) -> Dict[Tuple[str, str], str]:
    """Map ``(table_lower, column_lower)`` to the catalog's declared data type."""
    types: Dict[Tuple[str, str], str] = {}
    for raw in (columns_text or "").splitlines():
        stripped = raw.lstrip("- ").strip()
        qualified = stripped.split(" - ")[0].strip()
        if "." not in qualified:
            continue
        table, _, column = qualified.partition(".")
        table, column = table.strip(), column.strip()
        if table and column:
            types[(table.lower(), column.lower())] = _extract_type(stripped)
    return types


def column_display_names(columns_text: str) -> Dict[Tuple[str, str], Tuple[str, str]]:
    """Map lower-cased ``(table, column)`` to the catalog's original casing."""
    names: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for raw in (columns_text or "").splitlines():
        stripped = raw.lstrip("- ").strip()
        qualified = stripped.split(" - ")[0].strip()
        if "." not in qualified:
            continue
        table, _, column = qualified.partition(".")
        table, column = table.strip(), column.strip()
        if table and column:
            names.setdefault((table.lower(), column.lower()), (table, column))
    return names


def is_text_type(data_type: str) -> bool:
    """True when a catalog data type holds free text.

    Unknown/blank types count as text: the catalog is curated by hand and often
    omits the type, and skipping resolution there would silently disable the
    feature for exactly the datasets that need it most.
    """
    normalized = (data_type or "").strip().lower()
    if not normalized:
        return True
    return any(t in normalized for t in _TEXT_TYPES)


def _looks_numeric(value: str) -> bool:
    return bool(re.fullmatch(r"[-+]?[\d,.\s]+%?", value.strip()))


def _needles(value: Any) -> List[str]:
    """Literal strings carried by a filter value (scalar or ``in`` list)."""
    items = value if isinstance(value, (list, tuple)) else [value]
    out: List[str] = []
    for item in items:
        if not isinstance(item, str):
            return []
        text = item.strip()
        if len(text) < 2 or _looks_numeric(text) or not tokenize(text):
            return []
        out.append(text)
    return out


# ── Matching a single literal against a set of values ─────────────────────────


@dataclass(frozen=True)
class _Classified:
    """How one literal relates to the values available for a column."""

    kind: str  # single | refinement | ambiguous | none
    values: Tuple[str, ...] = ()
    exact: bool = False


def classify(needle: str, values: Sequence[str], threshold: float) -> _Classified:
    """Decide what *needle* means among *values*.

    Shared by the target column and the sibling-column search so both honour the
    same rule about when widening to ``IN`` is safe.
    """
    canonical = exact_value(needle, values)
    if canonical is not None:
        return _Classified("single", (canonical,), exact=True)

    matches = match_values(needle, values, limit=_MAX_IN_VALUES + 1, threshold=threshold)
    covered = [m for m in matches if m.covers_needle]

    if len(covered) == 1:
        return _Classified("single", (covered[0].value,))
    if is_refinement_set(needle, covered) and len(covered) <= _MAX_IN_VALUES:
        return _Classified("refinement", tuple(m.value for m in covered))
    if matches:
        return _Classified("ambiguous", tuple(m.value for m in matches[:_MAX_SUGGESTIONS]))
    return _Classified("none")


# ── Per-filter resolution outcome ─────────────────────────────────────────────


@dataclass
class _Outcome:
    """What resolving one filter produced."""

    filter: Dict[str, Any]
    status: str  # verified | rewritten | ambiguous | not_found | unverified
    assumption: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _Ctx:
    """Everything a single filter resolution needs, resolved once per request."""

    probe: PowerBiValueProbe
    source_key: str
    scope: str
    user_id: str
    types: Dict[Tuple[str, str], str]
    display: Dict[Tuple[str, str], Tuple[str, str]]
    table_columns: Dict[str, List[str]]
    dlp_re: Any
    dlp_enabled: bool
    max_domain_values: int
    threshold: float
    cross_column: bool
    # MCP candidates are merely a search accelerator. A candidate is accepted
    # only after the delegated Power BI probe confirms it is visible to the
    # asking user, so MCP metadata can never bypass RLS.
    mcp_search: Optional[McpSearch] = None


def _governed(ctx: _Ctx, column: str) -> bool:
    return bool(ctx.dlp_enabled and is_governed_name(ctx.dlp_re, column))


async def _domain(ctx: _Ctx, table: str, column: str) -> Optional[ValueDomain]:
    """Fetch (or reuse) the distinct values of one column; None if unreadable.

    A failed probe is never cached: doing so would turn one transient Power BI
    error into a value-blind window for the whole TTL, including the retry the
    feedback router makes precisely because the first attempt was unverified.
    """
    key = value_domain_cache.key(ctx.source_key, ctx.user_id, table, column, ctx.scope)
    cached = value_domain_cache.get(key)
    if cached is not None:
        return cached
    result: ProbeResult = await ctx.probe.distinct_values(
        table, column, limit=ctx.max_domain_values
    )
    if not result.ok:
        return None
    domain = ValueDomain(values=result.values, complete=result.complete)
    if domain.values:
        value_domain_cache.put(key, domain)
    return domain


def _sibling_columns(ctx: _Ctx, table: str, exclude: str) -> List[Tuple[str, str]]:
    """Text columns of *table* that could plausibly hold a user-named value."""
    candidates = ctx.table_columns.get(table.lower(), [])
    scored: List[Tuple[int, Tuple[str, str]]] = []
    for column_lower in candidates:
        if column_lower == exclude.lower():
            continue
        key = (table.lower(), column_lower)
        if not is_text_type(ctx.types.get(key, "")):
            continue
        table_display, column_display = ctx.display.get(key, (table, column_lower))
        if _governed(ctx, column_display):
            continue
        # A "…Name"/"…Model" column is far likelier to hold the value than a
        # free-text description, so probe those first within the small budget.
        rank = 0 if any(h in column_lower for h in _NAMEY_HINTS) else 1
        scored.append((rank, (table_display, column_display)))
    scored.sort(key=lambda item: item[0])
    return [pair for _, pair in scored[:_MAX_CROSS_COLUMNS]]


async def _cross_column_lookup(
    ctx: _Ctx, table: str, column: str, needle: str
) -> Tuple[List[Tuple[str, str, _Classified]], bool]:
    """Search sibling columns for *needle*.

    Returns the hits and whether every sibling was searched conclusively. An
    inconclusive search must not become "that value does not exist anywhere":
    the caller has to stay silent rather than assert an absence it cannot see.
    """
    siblings = _sibling_columns(ctx, table, column)
    fragments = search_tokens(needle)
    if not siblings or not fragments:
        return [], True

    async def probe_one(pair: Tuple[str, str]):
        sib_table, sib_column = pair
        result = await ctx.probe.contains_values(
            sib_table, sib_column, fragments, limit=_CROSS_COLUMN_ROWS
        )
        # A failed or truncated search is not evidence either way, and a
        # truncated one must never become an IN set built from a partial view.
        if not result.ok or not result.complete:
            return None
        hit = classify(needle, result.values, ctx.threshold)
        return (sib_table, sib_column, hit) if hit.kind != "none" else "empty"

    results = await asyncio.gather(*(probe_one(p) for p in siblings), return_exceptions=True)
    hits: List[Tuple[str, str, _Classified]] = []
    conclusive = True
    for result in results:
        if isinstance(result, Exception):
            logger.info("dax_entity_resolver: cross-column probe failed: %s", result)
            conclusive = False
        elif result is None:
            conclusive = False
        elif result != "empty":
            hits.append(result)
    return hits, conclusive


def _apply(filter_dict: Dict[str, Any], values: Sequence[str], *, target: str) -> Dict[str, Any]:
    """Return a copy of *filter_dict* bound to verified value(s)."""
    updated = dict(filter_dict)
    updated["target"] = target
    if len(values) == 1:
        updated["op"] = "equals"
        updated["value"] = values[0]
    else:
        updated["op"] = "in"
        updated["value"] = list(values)
    updated["resolved"] = True
    return updated


def _unverified(filter_dict: Dict[str, Any], reason: str) -> _Outcome:
    return _Outcome(filter_dict, "unverified", detail={"reason": reason})


def _normalise_typed_plan_filters(
    plan: Dict[str, Any],
    filters: List[Any],
    types: Dict[Tuple[str, str], str],
) -> Tuple[List[Any], List[Dict[str, Any]]]:
    """Normalize DAX date/number operands before entity lookup.

    The entity resolver intentionally skips ranges because a wrong number/date
    is not a spelling problem. It still needs deterministic parsing and range
    ordering checks, otherwise the generator receives unvalidated operands.
    """
    out: List[Any] = []
    errors: List[Dict[str, Any]] = []
    for filter_dict in filters:
        if not isinstance(filter_dict, dict):
            out.append(filter_dict)
            continue
        parsed = parse_target(filter_dict.get("target"))
        if not parsed:
            out.append(filter_dict)
            continue
        data_type = types.get((parsed[0].lower(), parsed[1].lower()), "")
        normalized, error = normalize_typed_filter(
            filter_dict,
            data_type,
            normalize_numeric_scalar=False,
        )
        if error:
            errors.append(
                {
                    "column": parsed[1],
                    "value": filter_dict.get("value"),
                    "candidates": [],
                    "reason": error,
                }
            )
        out.append(normalized)
    return out, errors


def _describe(needle: str, column: str, values: Sequence[str]) -> str:
    """One stated assumption for a literal that was corrected or widened."""
    if len(values) == 1:
        return f"Interpreted '{needle}' as {column} '{values[0]}'."
    preview = ", ".join(values[:3]) + ("…" if len(values) > 3 else "")
    return f"'{needle}' matched {len(values)} values in {column} ({preview}); included all of them."


async def _resolve_one(ctx: _Ctx, filter_dict: Dict[str, Any]) -> _Outcome:
    """Ground a single filter's literal(s) against the model."""
    needles = _needles(filter_dict.get("value"))
    parsed = parse_target(filter_dict.get("target"))
    if not parsed:
        return _unverified(filter_dict, "unparseable target")
    table, column = parsed
    key = (table.lower(), column.lower())
    table, column = ctx.display.get(key, (table, column))
    target = f"'{table}'[{column}]"

    if _governed(ctx, column):
        return _unverified(filter_dict, "governed column")

    # A catalog-side fuzzy search often finds a typo faster than reading a
    # high-cardinality Power BI column. Treat it as a *candidate only*: verify
    # the one suggested value using the user's delegated model query before
    # changing the plan. Multiple candidates are intentionally left for the
    # normal RLS-scoped resolver to disambiguate.
    if ctx.mcp_search and len(needles) == 1:
        try:
            candidates = list(await ctx.mcp_search(table, column, needles[0]))
        except Exception:  # noqa: BLE001 - optional acceleration fails open
            candidates = []
        if len(candidates) == 1:
            probe_result = await ctx.probe.contains_values(
                table,
                column,
                search_tokens(candidates[0]),
                limit=_MAX_SUGGESTIONS,
            )
            canonical = (
                exact_value(candidates[0], probe_result.values)
                if probe_result.ok else None
            )
            if canonical is not None:
                updated = _apply(filter_dict, [canonical], target=target)
                if canonical == needles[0]:
                    return _Outcome(updated, "verified")
                return _Outcome(
                    updated,
                    "rewritten",
                    assumption=_describe(needles[0], column, [canonical]),
                )

    domain = await _domain(ctx, table, column)
    if domain is None:
        return _unverified(filter_dict, "probe error")
    if not domain.values:
        return _unverified(filter_dict, "no values returned")

    # A truncated read is only the alphabetically first values, so it can neither
    # prove absence nor rank candidates. An exact hit inside it is still a real
    # value, so honour that and fail open on everything else.
    if not domain.complete:
        canonical = [exact_value(n, domain.values) for n in needles]
        if needles and all(c is not None for c in canonical):
            values = [c for c in canonical if c is not None]
            if values == needles:
                return _Outcome(_apply(filter_dict, values, target=target), "verified")
            return _Outcome(
                _apply(filter_dict, values, target=target),
                "rewritten",
                assumption=_describe(needles[0], column, values),
            )
        return _unverified(filter_dict, "domain too large")

    resolved: List[str] = []
    assumptions: List[str] = []
    for needle in needles:
        hit = classify(needle, domain.values, ctx.threshold)

        if hit.kind in ("single", "refinement"):
            # Two literals in one `in` list can legitimately land on the same
            # values ("Mountain 300" and "Mountain-300 Black, 38"); emitting the
            # duplicate would not change the answer but would look like a bug.
            for value in hit.values:
                if value not in resolved:
                    resolved.append(value)
            if not (hit.exact and list(hit.values) == [needle]):
                assumptions.append(_describe(needle, column, list(hit.values)))
            continue

        if hit.kind == "ambiguous":
            return _Outcome(
                filter_dict,
                "ambiguous",
                detail={
                    "target": target,
                    "column": column,
                    "value": needle,
                    "candidates": list(hit.values),
                },
            )

        # Nothing in this column. Only a lone literal is worth chasing across
        # sibling columns: splitting one filter over several columns would
        # change the query's meaning, not just its spelling.
        if ctx.cross_column and len(needles) == 1:
            outcome = await _resolve_elsewhere(ctx, filter_dict, table, column, needle)
            if outcome is not None:
                return outcome

        nearest = match_values(
            needle, domain.values, limit=_MAX_SUGGESTIONS, threshold=_SUGGESTION_THRESHOLD
        )
        return _Outcome(
            filter_dict,
            "not_found",
            detail={
                "target": target,
                "column": column,
                "value": needle,
                "candidates": [m.value for m in nearest],
            },
        )

    if not resolved:
        return _unverified(filter_dict, "no literals to resolve")
    if len(resolved) > _MAX_IN_VALUES:
        # Per-literal widening is capped, but several literals can still add up
        # to a filter so broad it no longer resembles the question asked.
        return _unverified(filter_dict, "too many matching values")
    if not assumptions:
        return _Outcome(_apply(filter_dict, resolved, target=target), "verified")
    return _Outcome(
        _apply(filter_dict, resolved, target=target),
        "rewritten",
        assumption=" ".join(assumptions),
    )


async def _resolve_elsewhere(
    ctx: _Ctx, filter_dict: Dict[str, Any], table: str, column: str, needle: str
) -> Optional[_Outcome]:
    """Retarget a filter onto a sibling column that actually holds the value."""
    hits, conclusive = await _cross_column_lookup(ctx, table, column, needle)
    if not conclusive:
        # Say nothing at all on a partial search. Answering from the columns
        # that happened to respond would let a transient probe failure decide
        # whether the user is interrupted, and the question asked would rest on
        # evidence we know to be incomplete. Staying unverified keeps the empty
        # result routing back here for a retry that can do better.
        return _unverified(filter_dict, "cross-column search incomplete")
    if not hits:
        return None

    # Retargeting a filter changes which column the question is about, so it is
    # only done when the evidence is unanimous: one column, one value, nothing
    # else competing. A CONTAINSSTRING search also returns a *subset* of its
    # column, so a multi-value set built from it could silently miss members —
    # only a single value is safe to apply without seeing the whole column.
    if len(hits) == 1 and hits[0][2].kind == "single":
        hit_table, hit_column, hit = hits[0]
        value = hit.values[0]
        return _Outcome(
            _apply(filter_dict, [value], target=f"'{hit_table}'[{hit_column}]"),
            "rewritten",
            assumption=(
                f"'{needle}' is not a {column}; matched it against "
                f"{hit_column} '{value}' instead."
            ),
        )

    # Several columns could serve, or one offers competing values. Either way it
    # is a question, and naming what was found elsewhere is far more useful than
    # reporting that the target column holds nothing.
    return _Outcome(
        filter_dict,
        "ambiguous",
        detail={
            "target": f"'{table}'[{column}]",
            "column": column,
            "value": needle,
            "columns": [f"{t}[{c}]" for t, c, _ in hits],
            "candidates": [v for _, _, hit in hits for v in hit.values][:_MAX_SUGGESTIONS],
        },
    )


# ── Clarification text (phase 1: prose, no new API contract) ──────────────────


def _quote_list(values: Sequence[str]) -> str:
    quoted = [f"'{v}'" for v in values]
    if len(quoted) <= 1:
        return "".join(quoted)
    return ", ".join(quoted[:-1]) + " or " + quoted[-1]


def build_clarification(
    ambiguous: Sequence[Dict[str, Any]], missing: Sequence[Dict[str, Any]]
) -> str:
    """Turn unresolved literals into one question the user can answer directly."""
    parts: List[str] = []
    for item in missing:
        candidates = item.get("candidates") or []
        if candidates:
            parts.append(
                f"I couldn't find \"{item['value']}\" in {item['column']}. "
                f"Did you mean {_quote_list(candidates)}?"
            )
        else:
            parts.append(
                f"I couldn't find \"{item['value']}\" in {item['column']}, and "
                "nothing in that column looks close to it."
            )
    for item in ambiguous:
        candidates = item.get("candidates") or []
        columns = item.get("columns") or []
        if len(columns) > 1:
            parts.append(
                f"\"{item['value']}\" matches values in more than one field "
                f"({', '.join(columns)}). Which did you mean?"
            )
        elif columns:
            parts.append(
                f"\"{item['value']}\" isn't a {item['column']}, but {columns[0]} has "
                f"{_quote_list(candidates)}. Which did you mean?"
            )
        elif candidates:
            # "could refer to" rather than "matches": the candidates may be near
            # misses for a value that does not exist at all.
            parts.append(
                f"\"{item['value']}\" could refer to several values in "
                f"{item['column']}: {_quote_list(candidates)}. Which did you mean?"
            )
    return " ".join(parts)


# ── Node ──────────────────────────────────────────────────────────────────────


def make_dax_entity_resolver(
    enabled: bool = True,
    *,
    dlp_enabled: bool = True,
    dlp_governed_columns: Optional[List[str]] = None,
    max_domain_values: int = 1000,
    match_threshold: float = 78.0,
    cross_column_enabled: bool = True,
    probe_factory: Optional[ProbeFactory] = None,
):
    """Return an async ``dax_entity_resolver`` node.

    The keyword arguments are construction-time defaults. When the agent seeds
    an admin-tunable snapshot into state (see ``DaxInsightsAgent``), that wins,
    so resolution can be retuned or switched off without a redeploy. A graph
    driven directly — tests, evals — supplies no snapshot and gets these.

    ``probe_factory`` is how the node reads values out of the model. It is
    injected so this node does not have to know where delegated tokens come
    from; omitting it yields a node that can never probe, which is the correct
    behaviour on a deployment with no connector platform.
    """
    dlp_re = build_dax_dlp_regex(dlp_governed_columns)
    build_probe = probe_factory or make_probe_factory()

    def _setting(state: DaxAgentState, key: str, fallback: Any) -> Any:
        value = state.get(key)
        return fallback if value is None else value

    async def dax_entity_resolver(state: DaxAgentState) -> Dict[str, Any]:
        attempts = int(state.get("entity_resolution_attempts") or 0)
        base: Dict[str, Any] = {"entity_resolution_attempts": attempts + 1}
        if not _setting(state, "entity_resolution_enabled", enabled):
            return base

        plan = state.get("query_plan") or {}
        filters = list(plan.get("filters") or [])
        columns_text = (state.get("metadata_bundle") or {}).get("columns", "")
        types = column_types(columns_text)
        filters, typed_errors = _normalise_typed_plan_filters(plan, filters, types)
        if typed_errors:
            clarification = build_clarification([], typed_errors)
            updated_plan = dict(plan)
            updated_plan["filters"] = filters
            return {
                **base,
                "query_plan": updated_plan,
                "entity_ambiguities": typed_errors,
                "unresolved_entities": [],
                "clarification": clarification,
                "answer": clarification,
                "clarification_required": True,
            }
        targets = [(i, f) for i, f in enumerate(filters) if _is_resolvable(f)][:_MAX_FILTERS]
        if not targets:
            # Clear any leftovers from an earlier pass: a stale unresolved entry
            # would keep sending the feedback router back here.
            logger.info("dax_entity_resolver: no literal filters to resolve")
            updates: Dict[str, Any] = {
                **base,
                "unresolved_entities": [],
                "entity_ambiguities": [],
            }
            if filters != list(plan.get("filters") or []):
                updated_plan = dict(plan)
                updated_plan["filters"] = filters
                updates["query_plan"] = updated_plan
            return updates

        probe = build_probe(state)
        identity = await _authorize(probe)
        if identity is None:
            # No usable probe, or the reader is no longer entitled to this
            # dataset. Either way nothing may be read — not even from cache.
            logger.info("dax_entity_resolver: cannot read model values — skipping")
            return {
                **base,
                "entity_ambiguities": [],
                "unresolved_entities": [
                    {"target": f.get("target"), "value": f.get("value"), "reason": "no probe"}
                    for _, f in targets
                ],
            }

        ctx = _Ctx(
            probe=probe,
            source_key=str(state.get("source_key") or ""),
            scope=f"{state.get('dataset_id') or ''}|{identity}",
            user_id=str(state.get("user_id") or ""),
            types=types,
            display=column_display_names(columns_text),
            table_columns=state.get("table_columns") or {},
            dlp_re=dlp_re,
            dlp_enabled=dlp_enabled,
            max_domain_values=int(
                _setting(state, "entity_max_domain_values", max_domain_values)
            ),
            threshold=float(
                _setting(state, "entity_match_threshold", match_threshold)
            ),
            cross_column=bool(
                _setting(state, "entity_cross_column_enabled", cross_column_enabled)
            ),
            mcp_search=_mcp_search_for_state(state),
        )

        # Only text columns are worth probing; skip the rest before spending a
        # round trip on them.
        resolvable: List[Tuple[int, Dict[str, Any]]] = []
        skipped: List[Dict[str, Any]] = []
        for index, filter_dict in targets:
            parsed = parse_target(filter_dict.get("target"))
            if parsed and not is_text_type(
                ctx.types.get((parsed[0].lower(), parsed[1].lower()), "")
            ):
                skipped.append(
                    {
                        "target": filter_dict.get("target"),
                        "value": filter_dict.get("value"),
                        "reason": "non-text column",
                    }
                )
                continue
            resolvable.append((index, filter_dict))

        if not resolvable:
            return {**base, "unresolved_entities": skipped, "entity_ambiguities": []}

        results = await asyncio.gather(
            *(_resolve_one(ctx, f) for _, f in resolvable), return_exceptions=True
        )

        outcomes: List[_Outcome] = []
        for (_, filter_dict), result in zip(resolvable, results):
            if isinstance(result, Exception):
                logger.warning(
                    "dax_entity_resolver: resolution failed for %s: %s",
                    filter_dict.get("target"), result,
                )
                outcomes.append(_unverified(filter_dict, "probe error"))
                continue
            outcomes.append(result)

        return _collect(state, base, plan, filters, resolvable, outcomes, skipped)

    return dax_entity_resolver


def _is_resolvable(filter_dict: Any) -> bool:
    """True when a plan filter carries free-text literal(s) worth verifying."""
    if not isinstance(filter_dict, dict):
        return False
    if str(filter_dict.get("op") or "").strip().lower() not in _RESOLVABLE_OPS:
        return False
    if filter_dict.get("resolved"):
        return False
    if str(filter_dict.get("value_kind") or "literal").strip().lower() != "literal":
        return False
    needles = _needles(filter_dict.get("value"))
    if not needles or len(needles) > _MAX_NEEDLES:
        return False
    return bool(parse_target(filter_dict.get("target")))


async def _authorize(probe: Optional[PowerBiValueProbe]) -> Optional[str]:
    """Cache scope for the current reader, or None if they may not read.

    Identity lookups touch the grant store, so a failure there must fail open
    like every other probe error rather than take the whole graph down.
    """
    if probe is None:
        return None
    try:
        return await probe.authorize()
    except Exception as exc:  # noqa: BLE001
        logger.info("dax_entity_resolver: authorization check failed: %s", exc)
        return None


def _mcp_search_for_state(state: DaxAgentState) -> Optional[McpSearch]:
    """Build an optional catalog candidate lookup for an MCP-backed catalog."""
    if state.get("catalog_source_used") != "mcp":
        return None
    visibility_checked: Optional[bool] = None

    async def search(table: str, column: str, needle: str) -> Sequence[str]:
        nonlocal visibility_checked
        try:
            from src.api import state as app_state
            client = app_state.mcp_catalog_client
            if client is None:
                return ()
            if visibility_checked is None:
                visibility_checked = await client.value_search_preserves_user_visibility()
            if not visibility_checked:
                return ()
            result = await client.search_column_values(
                str(state.get("source_key") or ""),
                table=table,
                column=column,
                query=needle,
                limit=_MAX_SUGGESTIONS,
            )
            return tuple(str(value) for value in result.get("values") or [])
        except Exception:  # noqa: BLE001 - catalog lookup must not affect RLS path
            logger.debug("dax_entity_resolver: MCP candidate search unavailable", exc_info=True)
            return ()

    return search


def make_probe_factory(
    token_provider_factory: Optional[TokenProviderFactory] = None,
) -> ProbeFactory:
    """Return the production probe factory for a given source of tokens."""
    provider_for = token_provider_factory or no_token_provider

    def build(state: DaxAgentState) -> Optional[PowerBiValueProbe]:
        return _build_probe(state, provider_for())

    return build


def _build_probe(
    state: DaxAgentState, provider: Optional[Any]
) -> Optional[PowerBiValueProbe]:
    """Wire a probe to this request's dataset and delegated token."""
    if provider is None:
        return None
    try:
        client = PowerBiDaxClient(
            workspace_id=state.get("workspace_id") or "",
            dataset_id=state.get("dataset_id") or "",
            api_base=settings.POWERBI_API_BASE,
            timeout=settings.POWERBI_EXECUTE_TIMEOUT_SECONDS,
        )
    except ValueError as exc:
        logger.info("dax_entity_resolver: Power BI connection unusable: %s", exc)
        return None

    auth_user_id = state.get("user_id")

    async def get_token() -> str:
        try:
            token = await provider.get_token_for_auth_user(auth_user_id)
        except PowerBiTokenError as exc:
            # The execution node raises the same error with a proper connect
            # prompt; here it just means the probe cannot run.
            logger.info("dax_entity_resolver: no Power BI token for probe: %s", exc)
            return ""
        return token.access_token

    async def get_identity() -> Optional[str]:
        # Minting a token re-runs the connector entitlement check, so this
        # doubles as the authorization gate for reading cached values.
        try:
            token = await provider.get_token_for_auth_user(auth_user_id)
        except PowerBiTokenError as exc:
            logger.info("dax_entity_resolver: not entitled to probe: %s", exc)
            return None
        if not token.access_token:
            return None
        return f"{token.grant_id}|{token.external_account or ''}"

    return PowerBiValueProbe(client, get_token, get_identity)


def _collect(
    state: DaxAgentState,
    base: Dict[str, Any],
    plan: Dict[str, Any],
    filters: List[Any],
    resolvable: List[Tuple[int, Dict[str, Any]]],
    outcomes: List[_Outcome],
    skipped: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Fold per-filter outcomes into a state update."""
    new_filters = list(filters)
    for (index, _), outcome in zip(resolvable, outcomes):
        new_filters[index] = outcome.filter

    assumptions: List[str] = []
    resolved: List[Dict[str, Any]] = []
    ambiguous: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = list(skipped)

    for (_, original), outcome in zip(resolvable, outcomes):
        if outcome.assumption:
            assumptions.append(outcome.assumption)
        if outcome.status in ("verified", "rewritten"):
            resolved.append(
                {
                    "target": outcome.filter.get("target"),
                    "raw_value": original.get("value"),
                    "value": outcome.filter.get("value"),
                    "status": outcome.status,
                }
            )
        elif outcome.status == "ambiguous":
            ambiguous.append(outcome.detail)
        elif outcome.status == "not_found":
            missing.append(outcome.detail)
        else:
            unresolved.append(
                {
                    "target": original.get("target"),
                    "value": original.get("value"),
                    "reason": outcome.detail.get("reason", "unverified"),
                }
            )

    updates: Dict[str, Any] = {
        **base,
        "resolved_entities": resolved,
        "entity_ambiguities": ambiguous + missing,
        "unresolved_entities": unresolved,
    }

    if new_filters != filters:
        updated_plan = dict(plan)
        updated_plan["filters"] = new_filters
        if assumptions:
            updated_plan["assumptions"] = list(plan.get("assumptions") or []) + assumptions
            updates["plan_assumptions"] = list(state.get("plan_assumptions") or []) + assumptions
        updates["query_plan"] = updated_plan

    if ambiguous or missing:
        clarification = build_clarification(ambiguous, missing)
        updates["clarification"] = clarification
        updates["answer"] = clarification
        updates["clarification_required"] = True
        logger.info(
            "dax_entity_resolver: asking user — %d ambiguous, %d not found",
            len(ambiguous), len(missing),
        )
    else:
        logger.info(
            "dax_entity_resolver: %d resolved, %d unverified",
            len(resolved), len(unresolved),
        )
    return updates


__all__ = [
    "build_clarification",
    "classify",
    "column_display_names",
    "column_types",
    "is_text_type",
    "make_dax_entity_resolver",
    "parse_target",
]
