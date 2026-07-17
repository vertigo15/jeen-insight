"""NL2SQL golden-set evaluation harness.

Scores three dimensions the review flagged as untested:

* **safety**       — does the guardrail stack block mutations, multi-statements,
                     DML-in-CTE, and governed columns while allowing clean reads?
* **groundedness** — does validation accept SQL that only touches catalogued
                     tables/columns and reject references to unknown ones?
* **route**        — does intent classification pick the right route? Greetings
                     are deterministic (local regex); the rest need a live LLM
                     (inject a classifier, otherwise those cases are skipped).

The safety and groundedness scorers are pure and deterministic — they reuse the
exact production guardrails (``connectors.base`` + the ``sqlglot_validate`` /
``dlp_check`` nodes) so the eval can't drift from runtime behaviour. That makes
this harness safe to run in CI without any Azure credentials.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

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
        order = ["route", "groundedness", "safety"]
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


# ── Orchestration ──────────────────────────────────────────────────────────


async def evaluate(
    dataset: Dict[str, Any],
    *,
    route_classifier: Optional[RouteClassifier] = None,
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
            else:
                logger.warning("evaluate: unknown case type %r (id=%s)", dim, case.get("id"))
        except Exception as exc:  # noqa: BLE001 — one bad case shouldn't abort the run
            report.add(CaseResult(
                case_id=case.get("id", "?"), dimension=str(dim), passed=False,
                actual="error", detail=f"scorer crashed: {exc}",
            ))
    return report
