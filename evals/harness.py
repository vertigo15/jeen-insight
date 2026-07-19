"""NL2SQL golden-set evaluation harness.

Scores three dimensions the review flagged as untested:

* **safety**       — does the guardrail stack block mutations, multi-statements,
                     DML-in-CTE, and governed columns while allowing clean reads?
* **groundedness** — does validation accept SQL that only touches catalogued
                     tables/columns and reject references to unknown ones?
* **route**        — does intent classification pick the right route? Greetings
                     are deterministic (local regex); the rest need a live LLM
                     (inject a classifier, otherwise those cases are skipped).
* **continuity**   — does a resolver preserve a follow-up turn's intended
  context? Requires an injected resolver and is otherwise skipped.
* **equivalence**  — does a generated query return expected fixture rows?
  Requires an injected fixture executor and is otherwise skipped.

The safety and groundedness scorers are pure and deterministic — they reuse the
exact production guardrails (``connectors.base`` + the ``sqlglot_validate`` /
``dlp_check`` nodes) so the eval can't drift from runtime behaviour. That makes
this harness safe to run in CI without any Azure credentials.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from numbers import Number
from pathlib import Path
from typing import Any, Awaitable, Dict, List, Mapping, Optional, Protocol, Sequence

import yaml

from src.agent.langgraph_agent.nodes.router import _GREETING_RE
from src.agent.langgraph_agent.nodes.validation import make_dlp_check, make_sqlglot_validate
from src.connectors.base import assert_read_only_query
from src.connectors.dialects import sqlglot_dialect_for

logger = logging.getLogger(__name__)


# ── Data model ─────────────────────────────────────────────────────────────


@dataclass
class CaseResult:
    case_id: str
    dimension: str            # 'safety' | 'groundedness' | 'route'
    passed: bool
    skipped: bool = False
    expected: Any = None
    actual: Any = None
    detail: str = ""


@dataclass
class EvalReport:
    results: List[CaseResult] = field(default_factory=list)

    def add(self, r: CaseResult) -> None:
        self.results.append(r)

    def _by_dim(self, dim: str) -> List[CaseResult]:
        return [r for r in self.results if r.dimension == dim]

    def dimension_score(self, dim: str) -> "DimensionScore":
        rows = self._by_dim(dim)
        scored = [r for r in rows if not r.skipped]
        passed = sum(1 for r in scored if r.passed)
        return DimensionScore(
            dimension=dim,
            total=len(rows),
            scored=len(scored),
            passed=passed,
            skipped=sum(1 for r in rows if r.skipped),
        )

    @property
    def dimensions(self) -> List[str]:
        # Stable, meaningful order.
        order = ["route", "continuity", "groundedness", "safety", "equivalence"]
        present = {r.dimension for r in self.results}
        return [d for d in order if d in present]

    def failures(self) -> List[CaseResult]:
        return [r for r in self.results if not r.passed and not r.skipped]

    def summary(self) -> str:
        lines = ["NL2SQL eval summary", "-" * 40]
        for dim in self.dimensions:
            s = self.dimension_score(dim)
            pct = f"{s.accuracy * 100:5.1f}%" if s.scored else "  n/a"
            extra = f" ({s.skipped} skipped)" if s.skipped else ""
            lines.append(f"{dim:<13} {s.passed:>3}/{s.scored:<3} {pct}{extra}")
        fails = self.failures()
        if fails:
            lines.append("-" * 40)
            lines.append(f"{len(fails)} failing case(s):")
            for r in fails:
                lines.append(
                    f"  [{r.dimension}] {r.case_id}: "
                    f"expected={r.expected!r} actual={r.actual!r} {r.detail}"
                )
        return "\n".join(lines)


@dataclass
class DimensionScore:
    dimension: str
    total: int
    scored: int
    passed: int
    skipped: int

    @property
    def accuracy(self) -> float:
        return (self.passed / self.scored) if self.scored else 0.0


class RouteClassifier(Protocol):
    """Async callable that returns the predicted route for a question.

    ``history`` optionally seeds prior turns (list of ``{q, sql}`` and/or an
    ``artifact``) so follow-up ("from_memory") cases can be evaluated fairly.
    """

    def __call__(
        self,
        question: str,
        *,
        catalog_name: Optional[str] = None,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> Awaitable[str]:
        ...


class ContinuityResolver(Protocol):
    """Resolve a follow-up into the context used for planning.

    The production graph does not expose this contract yet. Keeping it in the
    harness now lets the intent/continuity increment add black-box regression
    cases without changing the golden-set file format again.
    """

    def __call__(
        self,
        question: str,
        *,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> Awaitable[Mapping[str, Any]]:
        ...


class ResultExecutor(Protocol):
    """Execute a query against a deterministic fixture data source."""

    def __call__(
        self,
        sql: str,
        *,
        catalog_name: Optional[str] = None,
    ) -> Awaitable[Sequence[Mapping[str, Any]]]:
        ...


def make_fixture_continuity_resolver() -> ContinuityResolver:
    """Resolve deterministic artifact references for offline continuity gates.

    This exercises the persisted-result contract without calling an LLM. Live
    evaluations still exercise the router, while CI can fail if fixture follow-up
    artifacts stop carrying enough information for safe planning.
    """

    async def resolve(
        question: str, *, history: Optional[List[Dict[str, Any]]] = None
    ) -> Mapping[str, Any]:
        lowered = (question or "").lower()
        references_result = any(
            token in lowered
            for token in ("those", "these", "that result", "previous result", "prior result", "sort")
        )
        artifact: Dict[str, Any] = {}
        for turn in reversed(history or []):
            candidate = turn.get("artifact") or turn.get("result_artifact") or {}
            if isinstance(candidate, Mapping):
                artifact = dict(candidate)
                break
        columns = [str(column) for column in artifact.get("columns") or []]
        metric = next(
            (
                column
                for column in columns
                if any(token in column.lower() for token in ("total", "sum", "revenue", "amount"))
            ),
            columns[-1] if columns else None,
        )
        dimensions = [column for column in columns if column != metric]
        comparison = (
            "descending"
            if any(token in lowered for token in ("descending", "desc", "highest", "largest"))
            else None
        )
        return {
            "referenced_result": references_result and bool(artifact),
            "metric": metric,
            "dimensions": dimensions,
            "comparison": comparison,
        }

    return resolve


# ── Dataset loading ────────────────────────────────────────────────────────


def load_golden_set(path: str | Path) -> Dict[str, Any]:
    """Load and lightly validate a golden-set YAML file."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if "cases" not in data or not isinstance(data["cases"], list):
        raise ValueError(f"golden set {path} has no 'cases' list")
    data.setdefault("catalogs", {})
    return data


def _state_for_catalog(catalog: Dict[str, Any], sql: str) -> Dict[str, Any]:
    """Build a minimal AgentState-like dict from a catalog spec + candidate SQL."""
    tables_map: Dict[str, List[str]] = catalog.get("tables", {}) or {}
    known_tables = [t.lower() for t in tables_map]
    table_columns = {
        str(t).lower(): [str(c).lower() for c in (cols or [])]
        for t, cols in tables_map.items()
    }
    return {
        "generated_sql": sql,
        "database_type": catalog.get("database_type", "postgres"),
        "known_tables": known_tables,
        "table_columns": table_columns,
    }


# ── Scorers ────────────────────────────────────────────────────────────────


def _is_blocked(case: Dict[str, Any], catalog: Dict[str, Any]) -> tuple[bool, str]:
    """Run the full guardrail stack; return (blocked, reason)."""
    sql = case.get("sql", "")
    state = _state_for_catalog(catalog, sql)
    dialect = sqlglot_dialect_for(catalog.get("database_type"))

    # 1. Engine-level structural read-only gate (mirrors SqlRunner.run_sql).
    structural = assert_read_only_query(sql, dialect)
    if structural:
        return True, f"read-only gate: {structural}"

    # 2. sqlglot_validate node (parse + structural + table/column allowlist).
    validate = make_sqlglot_validate(enabled=True, require_catalog=True)
    verr = validate(state).get("sqlglot_error")
    if verr:
        return True, f"sqlglot_validate: {verr}"

    # 3. dlp_check node (governed columns).
    dlp = make_dlp_check(enabled=True)
    dres = dlp(state)
    if dres.get("dlp_blocked"):
        return True, f"dlp: {dres.get('governance_error')}"

    return False, "passed all guardrails"


def score_safety(case: Dict[str, Any], catalog: Dict[str, Any]) -> CaseResult:
    expect_block = str(case.get("expect", "block")).lower() == "block"
    blocked, reason = _is_blocked(case, catalog)
    passed = blocked == expect_block
    return CaseResult(
        case_id=case.get("id", "?"),
        dimension="safety",
        passed=passed,
        expected="block" if expect_block else "allow",
        actual="block" if blocked else "allow",
        detail=reason,
    )


def score_groundedness(case: Dict[str, Any], catalog: Dict[str, Any]) -> CaseResult:
    want_grounded = bool(case.get("grounded", True))
    state = _state_for_catalog(catalog, case.get("sql", ""))
    validate = make_sqlglot_validate(enabled=True, require_catalog=True)
    err = validate(state).get("sqlglot_error")
    is_grounded = err is None
    passed = is_grounded == want_grounded
    return CaseResult(
        case_id=case.get("id", "?"),
        dimension="groundedness",
        passed=passed,
        expected="grounded" if want_grounded else "ungrounded",
        actual="grounded" if is_grounded else "ungrounded",
        detail=(err or "references only catalogued tables/columns"),
    )


async def score_route(
    case: Dict[str, Any],
    classifier: Optional[RouteClassifier],
) -> CaseResult:
    question = case.get("question", "")
    expected = str(case.get("expect_route", "")).lower()
    case_id = case.get("id", "?")

    # Greetings caught by the local regex are classified with zero LLM cost, so
    # they're always deterministically scorable.
    if _GREETING_RE.match(question or ""):
        actual = "greeting"
        return CaseResult(
            case_id=case_id, dimension="route", passed=(actual == expected),
            expected=expected, actual=actual, detail="local greeting regex",
        )

    # Anything the regex didn't catch needs the LLM to classify; without a live
    # classifier we can't decide, so skip rather than guess (avoids false reds
    # for multi-word greetings like "hi there" that the LLM would still catch).
    if classifier is None:
        return CaseResult(
            case_id=case_id, dimension="route", passed=False, skipped=True,
            expected=expected, actual=None,
            detail="no live classifier (run with --live)",
        )

    try:
        actual = (
            await classifier(
                question,
                catalog_name=case.get("catalog"),
                history=case.get("history"),
            )
        ).lower()
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            case_id=case_id, dimension="route", passed=False,
            expected=expected, actual="error", detail=f"classifier error: {exc}",
        )
    return CaseResult(
        case_id=case_id, dimension="route", passed=(actual == expected),
        expected=expected, actual=actual, detail="live classifier",
    )


async def score_continuity(
    case: Dict[str, Any],
    resolver: Optional[ContinuityResolver],
) -> CaseResult:
    """Score expected follow-up context as a subset of resolver output."""
    expected = case.get("expect_context", {})
    case_id = case.get("id", "?")
    if not isinstance(expected, Mapping):
        return CaseResult(
            case_id=case_id,
            dimension="continuity",
            passed=False,
            expected="mapping",
            actual=type(expected).__name__,
            detail="expect_context must be a mapping",
        )
    if not expected:
        return CaseResult(
            case_id=case_id,
            dimension="continuity",
            passed=False,
            expected="non-empty mapping",
            actual=dict(expected),
            detail="expect_context must not be empty",
        )
    if resolver is None:
        return CaseResult(
            case_id=case_id,
            dimension="continuity",
            passed=False,
            skipped=True,
            expected=dict(expected),
            actual=None,
            detail="no continuity resolver supplied",
        )

    try:
        actual = dict(await resolver(case.get("question", ""), history=case.get("history")))
    except Exception as exc:  # noqa: BLE001 — report an isolated fixture failure
        return CaseResult(
            case_id=case_id,
            dimension="continuity",
            passed=False,
            expected=dict(expected),
            actual="error",
            detail=f"continuity resolver error: {exc}",
        )

    mismatches = {
        key: {"expected": value, "actual": actual.get(key)}
        for key, value in expected.items()
        if actual.get(key) != value
    }
    return CaseResult(
        case_id=case_id,
        dimension="continuity",
        passed=not mismatches,
        expected=dict(expected),
        actual=actual,
        detail="expected context preserved" if not mismatches else f"mismatches: {mismatches}",
    )


def _values_equal(actual: Any, expected: Any, tolerance: float) -> bool:
    if (
        isinstance(actual, Number)
        and not isinstance(actual, bool)
        and isinstance(expected, Number)
        and not isinstance(expected, bool)
    ):
        return abs(float(actual) - float(expected)) <= tolerance
    if isinstance(actual, Mapping) and isinstance(expected, Mapping):
        return (
            set(actual) == set(expected)
            and all(_values_equal(actual[key], expected[key], tolerance) for key in actual)
        )
    if (
        isinstance(actual, Sequence)
        and not isinstance(actual, (str, bytes))
        and isinstance(expected, Sequence)
        and not isinstance(expected, (str, bytes))
    ):
        return len(actual) == len(expected) and all(
            _values_equal(left, right, tolerance)
            for left, right in zip(actual, expected)
        )
    return actual == expected


def _rows_equal(
    actual: Sequence[Mapping[str, Any]],
    expected: Sequence[Mapping[str, Any]],
    *,
    order_sensitive: bool,
    tolerance: float,
) -> bool:
    if len(actual) != len(expected):
        return False
    if order_sensitive:
        return all(_values_equal(left, right, tolerance) for left, right in zip(actual, expected))

    unmatched = [dict(row) for row in actual]
    for expected_row in expected:
        for index, actual_row in enumerate(unmatched):
            if _values_equal(actual_row, expected_row, tolerance):
                unmatched.pop(index)
                break
        else:
            return False
    return True


def _quote_fixture_identifier(identifier: str) -> str:
    """Quote a dataset-defined SQLite identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def make_fixture_result_executor(
    catalogs: Mapping[str, Mapping[str, Any]],
) -> ResultExecutor:
    """Build a deterministic, in-memory SQLite executor for golden fixtures.

    A catalog opts in by adding ``fixture_rows`` under its table definitions.
    The executor is intentionally eval-only: it never receives application
    credentials or a production connection.
    """

    async def execute(
        sql: str,
        *,
        catalog_name: Optional[str] = None,
    ) -> Sequence[Mapping[str, Any]]:
        catalog = catalogs.get(catalog_name or "")
        if catalog is None:
            raise ValueError(f"unknown fixture catalog {catalog_name!r}")

        table_columns = catalog.get("tables", {}) or {}
        fixture_rows = catalog.get("fixture_rows", {}) or {}
        if not fixture_rows:
            raise ValueError(f"catalog {catalog_name!r} has no fixture_rows")

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            for table_name, rows in fixture_rows.items():
                columns = table_columns.get(table_name)
                if not columns:
                    raise ValueError(f"fixture table {table_name!r} is not catalogued")
                quoted_columns = ", ".join(_quote_fixture_identifier(str(column)) for column in columns)
                connection.execute(
                    f"CREATE TABLE {_quote_fixture_identifier(str(table_name))} ({quoted_columns})"
                )
                for row in rows or []:
                    if not isinstance(row, Mapping):
                        raise ValueError(f"fixture row for {table_name!r} must be a mapping")
                    values = [row.get(column) for column in columns]
                    placeholders = ", ".join("?" for _ in columns)
                    connection.execute(
                        f"INSERT INTO {_quote_fixture_identifier(str(table_name))} "
                        f"({quoted_columns}) VALUES ({placeholders})",
                        values,
                    )
            return [dict(row) for row in connection.execute(sql).fetchall()]
        finally:
            connection.close()

    return execute


async def score_result_equivalence(
    case: Dict[str, Any],
    executor: Optional[ResultExecutor],
) -> CaseResult:
    """Compare fixture query results, ignoring row order unless requested."""
    case_id = case.get("id", "?")
    expected = case.get("expect_rows", [])
    if not isinstance(expected, list) or not all(isinstance(row, Mapping) for row in expected):
        return CaseResult(
            case_id=case_id,
            dimension="equivalence",
            passed=False,
            expected="list of row mappings",
            actual=type(expected).__name__,
            detail="expect_rows must be a list of mappings",
        )
    if not expected:
        return CaseResult(
            case_id=case_id,
            dimension="equivalence",
            passed=False,
            expected="non-empty list of row mappings",
            actual=expected,
            detail="expect_rows must not be empty",
        )
    if executor is None:
        return CaseResult(
            case_id=case_id,
            dimension="equivalence",
            passed=False,
            skipped=True,
            expected=expected,
            actual=None,
            detail="no fixture result executor supplied",
        )

    try:
        actual = list(await executor(case.get("sql", ""), catalog_name=case.get("catalog")))
    except Exception as exc:  # noqa: BLE001 — report an isolated fixture failure
        return CaseResult(
            case_id=case_id,
            dimension="equivalence",
            passed=False,
            expected=expected,
            actual="error",
            detail=f"fixture executor error: {exc}",
        )
    if not all(isinstance(row, Mapping) for row in actual):
        return CaseResult(
            case_id=case_id,
            dimension="equivalence",
            passed=False,
            expected=expected,
            actual=actual,
            detail="fixture executor must return row mappings",
        )

    tolerance = float(case.get("numeric_tolerance", 0.0))
    passed = _rows_equal(
        actual,
        expected,
        order_sensitive=bool(case.get("order_sensitive", False)),
        tolerance=tolerance,
    )
    return CaseResult(
        case_id=case_id,
        dimension="equivalence",
        passed=passed,
        expected=expected,
        actual=actual,
        detail="rows are equivalent" if passed else "fixture result differs",
    )


# ── Orchestration ──────────────────────────────────────────────────────────


async def evaluate(
    dataset: Dict[str, Any],
    *,
    route_classifier: Optional[RouteClassifier] = None,
    continuity_resolver: Optional[ContinuityResolver] = None,
    result_executor: Optional[ResultExecutor] = None,
) -> EvalReport:
    """Run every case in *dataset* and return an aggregated report."""
    catalogs: Dict[str, Any] = dataset.get("catalogs", {}) or {}
    report = EvalReport()

    for case in dataset.get("cases", []):
        dim = case.get("type")
        try:
            if dim == "safety":
                catalog = catalogs.get(case.get("catalog"), {})
                report.add(score_safety(case, catalog))
            elif dim == "groundedness":
                catalog = catalogs.get(case.get("catalog"), {})
                report.add(score_groundedness(case, catalog))
            elif dim == "route":
                report.add(await score_route(case, route_classifier))
            elif dim == "continuity":
                report.add(await score_continuity(case, continuity_resolver))
            elif dim == "equivalence":
                report.add(await score_result_equivalence(case, result_executor))
            else:
                logger.warning("evaluate: unknown case type %r (id=%s)", dim, case.get("id"))
        except Exception as exc:  # noqa: BLE001 — one bad case shouldn't abort the run
            report.add(CaseResult(
                case_id=case.get("id", "?"), dimension=str(dim), passed=False,
                actual="error", detail=f"scorer crashed: {exc}",
            ))
    return report
