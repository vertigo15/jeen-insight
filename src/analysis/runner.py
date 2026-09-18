"""Where a skill actually executes.

``execute_skill`` is the single, synchronous entry point: validated params +
aggregate rows in, envelope (or guard refusal) out. Every runner calls it —
directly in-process (tests), in a resource-limited child process (development)
or inside the ``jeen-insights-analytics`` container (production, P5). The
graph node never knows which.

Only the sandbox runner is a production path. ``LocalSubprocessRunner`` refuses
to construct unless ``JEEN_DEV_MODE`` is true; see :func:`build_runner`.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from pydantic import ValidationError

from src.analysis.contracts import (
    ADDITIVE_AGGS,
    ChartSpec,
    GuardResult,
    ResultEnvelope,
    SkillSpec,
    get_skill,
    parse_params,
)
from src.analysis.engines.common import RunContext
from src.analysis.guards import (
    SERIES_COUNT_MAX,
    arm_count,
    cardinality,
    cohort_size,
    failed,
    feature_count,
    group_size,
    retention_history,
    run_post_sql_guards,
    series_count,
    slices,
)
from src.analysis.series import format_period, partial_tail_sentence, prepare_series, split_series_groups

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30
# statsforecast's first import JIT-compiles through numba (~6 s warm, longer
# cold), so a per-run child process needs more headroom than the sandbox.
LOCAL_TIMEOUT_SECONDS = 60
DEFAULT_MEMORY_MB = 1024
MAX_SERIES_ROWS = 5000


@dataclass
class RunOutcome:
    """What a runner hands back. Exactly one of the three is populated."""

    status: str  # ok | guard_failed | error
    envelope: Optional[ResultEnvelope] = None
    guard_results: List[GuardResult] = field(default_factory=list)
    error: Optional[str] = None
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_json(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "envelope": self.envelope.model_dump(mode="json") if self.envelope else None,
            "guard_results": [g.model_dump(mode="json") for g in self.guard_results],
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
        }

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "RunOutcome":
        env = data.get("envelope")
        return cls(
            status=str(data.get("status") or "error"),
            envelope=ResultEnvelope.model_validate(env) if env else None,
            guard_results=[GuardResult.model_validate(g) for g in data.get("guard_results") or []],
            error=data.get("error"),
            elapsed_ms=int(data.get("elapsed_ms") or 0),
        )


class GuardRefused(Exception):
    def __init__(self, results: List[GuardResult]):
        super().__init__("guard refused")
        self.results = results


# ── The one entry point ───────────────────────────────────────────────────────


def execute_skill(
    skill: str,
    params: Dict[str, Any],
    series: Dict[str, Any],
    *,
    override_guards: bool = False,
    context: Optional[Dict[str, Any]] = None,
) -> RunOutcome:
    """Validate → prepare series → post-SQL guards → engine. Never raises.

    ``series`` is ``{"columns": [...], "rows": [...]}`` as returned by the
    ``SqlRunner`` (``ts``/``value`` columns). ``override_guards`` runs past a
    refused guard and marks the envelope low-confidence.
    """
    import time

    t0 = time.monotonic()
    ctx_raw = dict(context or {})
    try:
        spec = get_skill(skill)
        typed = parse_params(skill, params)
    except (KeyError, ValidationError) as exc:
        return RunOutcome(status="error", error=f"invalid analysis request: {exc}")

    rows = list(series.get("rows") or [])
    columns = list(series.get("columns") or [])
    row_limit = MAX_SERIES_ROWS if spec.family != "entity" else max(MAX_SERIES_ROWS, int(getattr(typed.entity, "row_cap", MAX_SERIES_ROWS)))
    if len(rows) > row_limit:
        return RunOutcome(status="error", error=f"too many rows for {spec.title}: {len(rows)} (max {row_limit})")
    if not rows:
        return RunOutcome(status="error", error="the query returned no rows for this request")

    def _ctx(low_confidence: bool) -> RunContext:
        return RunContext(
            sql=ctx_raw.get("sql"),
            query_ts=ctx_raw.get("query_ts"),
            filters_summary=str(ctx_raw.get("filters_summary") or ""),
            runner=str(ctx_raw.get("runner") or "in_process"),
            low_confidence=low_confidence,
        )

    try:
        if spec.family == "series":
            return _run_series_family(spec, typed, rows, columns, override_guards=override_guards,
                                      base_low_confidence=bool(ctx_raw.get("low_confidence")), make_ctx=_ctx, t0=t0,
                                      data_end=ctx_raw.get("data_end"))
        if spec.family == "contribution":
            return _run_contribution(spec, typed, rows, columns, override_guards=override_guards,
                                     base_low_confidence=bool(ctx_raw.get("low_confidence")), make_ctx=_ctx, t0=t0)
        if spec.family == "cohort":
            return _run_cohort(spec, typed, rows, columns, override_guards=override_guards,
                               base_low_confidence=bool(ctx_raw.get("low_confidence")), make_ctx=_ctx, t0=t0)
        if spec.family == "experiment":
            return _run_experiment(spec, typed, rows, columns, override_guards=override_guards,
                                   base_low_confidence=bool(ctx_raw.get("low_confidence")), make_ctx=_ctx, t0=t0)
        return _run_entity(spec, typed, rows, columns, override_guards=override_guards,
                           base_low_confidence=bool(ctx_raw.get("low_confidence")), make_ctx=_ctx, t0=t0)
    except Exception as exc:  # noqa: BLE001
        # Full traceback for operators; the user-facing text names the failure
        # class only, never library internals or data values.
        logger.exception("execute_skill: %s failed", spec.engine)
        return RunOutcome(status="error", error=f"{spec.title} could not complete ({type(exc).__name__}). Try a coarser grain or a different window.",
                          elapsed_ms=int((time.monotonic() - t0) * 1000))


def _elapsed(t0: float) -> int:
    import time

    return int((time.monotonic() - t0) * 1000)


def _extra_values(spec: SkillSpec, typed: Any):
    if spec.name == "correlation":
        return [("value2", typed.other_agg in ADDITIVE_AGGS)]
    return []


def _partial_tail_guard(sf) -> GuardResult:
    """Informational: names the incomplete trailing period that was set aside."""
    tail = sf.partial_tail
    return GuardResult(
        name="partial_tail", passed=True, overridable=False,
        detail=f"{format_period(tail.ts, sf.grain)} set aside as incomplete ({tail.reason}); "
               f"{sf.n} complete {sf.period_label(plural=True)} analysed",
        observed=float(sf.n),
    )


def _run_one_series(spec: SkillSpec, typed: Any, rows, columns, *, override_guards, base_low_confidence, make_ctx,
                    guard_prefix: List[GuardResult] = (), data_end: Any = None):
    """Prepare → guards → engine for one series. Returns (envelope | None, guards)."""
    sf = prepare_series(rows, typed.series, columns=columns, extra_values=_extra_values(spec, typed), data_end=data_end)
    if sf.n == 0:
        return None, [GuardResult(name="series_length", passed=False, detail="0 of 12 periods", overridable=False)]
    horizon = getattr(typed, "horizon", None)
    guards = list(guard_prefix)
    if sf.partial_tail is not None:
        guards.append(_partial_tail_guard(sf))
    guards += run_post_sql_guards(sf, guard_names=list(spec.guards), horizon=horizon)
    refused = failed(guards)
    if refused and not override_guards:
        return None, guards
    ctx = make_ctx(base_low_confidence or bool(refused))
    engine = importlib.import_module(spec.engine)
    envelope = engine.run(typed, sf, ctx, guard_results=guards)
    if sf.partial_tail is not None:
        # Engines that already explain the set-aside period (forecast) are left
        # alone; every other series skill gets the one shared sentence.
        sentence = partial_tail_sentence(sf)
        if sentence and not any(sentence in c for c in envelope.caveats):
            envelope.caveats.append(sentence)
    return envelope, guards


def _run_series_family(spec, typed, rows, columns, *, override_guards, base_low_confidence, make_ctx, t0,
                       data_end: Any = None) -> RunOutcome:
    group_by = getattr(typed.series, "group_by", None)
    if not group_by:
        envelope, guards = _run_one_series(spec, typed, rows, columns, override_guards=override_guards,
                                           base_low_confidence=base_low_confidence, make_ctx=make_ctx, data_end=data_end)
        if envelope is None:
            return RunOutcome(status="guard_failed", guard_results=guards, elapsed_ms=_elapsed(t0))
        return RunOutcome(status="ok", envelope=envelope, elapsed_ms=_elapsed(t0))

    # ── Multi-series: one engine run per distinct value, merged into one table ──
    groups = split_series_groups(rows, columns)
    count_guard = series_count(len(groups), group_by)
    if not count_guard.passed and not override_guards:
        return RunOutcome(status="guard_failed", guard_results=[count_guard], elapsed_ms=_elapsed(t0))
    if not count_guard.passed:
        def _mass(items):
            return sum(abs(float(r.get("value") or 0)) for r in items if isinstance(r, dict))
        groups = dict(sorted(groups.items(), key=lambda kv: -_mass(kv[1]))[:SERIES_COUNT_MAX])
    envelopes: Dict[str, ResultEnvelope] = {}
    skipped: List[str] = []
    all_guards: List[GuardResult] = [count_guard]
    for sid, group_rows in groups.items():
        envelope, guards = _run_one_series(spec, typed, group_rows, columns, override_guards=override_guards,
                                           base_low_confidence=base_low_confidence or not count_guard.passed, make_ctx=make_ctx,
                                           data_end=data_end)
        for g in guards:
            all_guards.append(g.model_copy(update={"name": f"{g.name}[{sid}]"}))
        if envelope is None:
            skipped.append(f"{sid} ({', '.join(g.name for g in failed(guards)) or 'no rows'})")
            continue
        envelopes[sid] = envelope
    if not envelopes:
        return RunOutcome(status="guard_failed", guard_results=all_guards, elapsed_ms=_elapsed(t0))

    top_id = max(envelopes, key=lambda s: abs(sum(float(r.get("actual") or r.get("value") or 0) for r in envelopes[s].rows)))
    top = envelopes[top_id]
    merged_rows: List[Dict[str, Any]] = []
    for sid, env in envelopes.items():
        merged_rows.extend({"series_id": sid, **row} for row in env.rows)
    chart = top.chart_spec.model_copy(update={"row_filter": {"column": "series_id", "value": top_id}})
    notes = list(top.details.notes)
    if skipped:
        notes.append(f"Series skipped by guards: {', '.join(skipped)}.")
    details = top.details.model_copy(update={"notes": notes})
    # The top series' caveats travel as-is; the others' set-aside periods would
    # otherwise change their history with no word to the reader.
    set_aside = [g.name[len("partial_tail["):-1] for g in all_guards if g.name.startswith("partial_tail[")]
    set_aside_note = (
        [f"An incomplete last period was set aside for {len(set_aside)} series ({', '.join(set_aside)}); "
         "see each series' partial_tail guard for the numbers."]
        if set_aside and set(set_aside) != {top_id} else []
    )
    facts = {
        "skill": spec.name, "group_by": group_by, "series_count": len(envelopes), "top_series": top_id,
        "series": {sid: env.facts for sid, env in envelopes.items()},
        "skipped": skipped,
    }
    merged = top.model_copy(update={
        "columns": ["series_id", *top.columns],
        "rows": merged_rows,
        "chart_spec": chart,
        "guard_results": all_guards,
        "low_confidence": any(e.low_confidence for e in envelopes.values()),
        "egress": top.egress.model_copy(update={"rows_sent_to_model": sum(e.egress.rows_sent_to_model for e in envelopes.values())}),
        "details": details,
        "facts": facts,
        "headline": f"{len(envelopes)} series by {group_by}; largest ({top_id}): {top.headline}",
        "caveats": [f"Each series was analysed separately; the chart shows {top_id}. Filter the rows table by series_id for the others."]
                   + set_aside_note + top.caveats,
        "params": typed.model_dump(mode="json"),
    })
    return RunOutcome(status="ok", envelope=merged, elapsed_ms=_elapsed(t0))


def _to_frame(rows, columns):
    import pandas as pd

    if rows and isinstance(rows[0], (list, tuple)) and columns:
        return pd.DataFrame(rows, columns=columns)
    return pd.DataFrame([dict(r) for r in rows])


def _run_contribution(spec, typed, rows, columns, *, override_guards, base_low_confidence, make_ctx, t0) -> RunOutcome:
    df = _to_frame(rows, columns)
    for col in ("dimension", "slice", "period", "value"):
        if col not in df.columns:
            return RunOutcome(status="error", error=f"contribution rows lack the '{col}' column", elapsed_ms=_elapsed(t0))
    n_slices = int(df[["dimension", "slice"]].drop_duplicates().shape[0])
    guards = [slices(n_slices, int(len(df)), dimensions=list(typed.dimensions))]
    refused = failed(guards)
    if refused and not override_guards:
        return RunOutcome(status="guard_failed", guard_results=guards, elapsed_ms=_elapsed(t0))
    ctx = make_ctx(base_low_confidence or bool(refused))
    engine = importlib.import_module(spec.engine)
    envelope = engine.run(typed, df, ctx, guard_results=guards)
    return RunOutcome(status="ok", envelope=envelope, elapsed_ms=_elapsed(t0))


def _run_cohort(spec, typed, rows, columns, *, override_guards, base_low_confidence, make_ctx, t0) -> RunOutcome:
    df = _to_frame(rows, columns)
    for col in ("cohort", "period", "active"):
        if col not in df.columns:
            return RunOutcome(status="error", error=f"cohort rows lack the '{col}' column", elapsed_ms=_elapsed(t0))
    activity = df[df["period"].notna()]
    n_cohorts = int(df["cohort"].nunique())
    if activity.empty:
        n_offsets = 0
    else:
        n_offsets = int(activity.groupby("cohort")["period"].nunique().max())
    guards = [cohort_size(n_cohorts), retention_history(n_offsets)]
    refused = failed(guards)
    if refused and not override_guards:
        return RunOutcome(status="guard_failed", guard_results=guards, elapsed_ms=_elapsed(t0))
    ctx = make_ctx(base_low_confidence or bool(refused))
    engine = importlib.import_module(spec.engine)
    envelope = engine.run(typed, df, ctx, guard_results=guards)
    return RunOutcome(status="ok", envelope=envelope, elapsed_ms=_elapsed(t0))


def _run_experiment(spec, typed, rows, columns, *, override_guards, base_low_confidence, make_ctx, t0) -> RunOutcome:
    import pandas as pd

    df = _to_frame(rows, columns)
    for col in ("arm", "n", "sum_x", "sum_x2"):
        if col not in df.columns:
            return RunOutcome(status="error", error=f"experiment rows lack the '{col}' column", elapsed_ms=_elapsed(t0))
    df = df[pd.to_numeric(df["n"], errors="coerce").fillna(0) > 0]
    n_arms = int(df["arm"].nunique())
    min_arm_n = int(pd.to_numeric(df["n"], errors="coerce").fillna(0).min()) if len(df) else 0
    guards = [arm_count(n_arms), group_size(min_arm_n)]
    refused = failed(guards)
    if refused and not override_guards:
        return RunOutcome(status="guard_failed", guard_results=guards, elapsed_ms=_elapsed(t0))
    ctx = make_ctx(base_low_confidence or bool(refused))
    engine = importlib.import_module(spec.engine)
    envelope = engine.run(typed, df, ctx, guard_results=guards)
    return RunOutcome(status="ok", envelope=envelope, elapsed_ms=_elapsed(t0))


def _run_entity(spec, typed, rows, columns, *, override_guards, base_low_confidence, make_ctx, t0) -> RunOutcome:
    import pandas as pd

    df = _to_frame(rows, columns)
    if "entity_key" not in df.columns:
        return RunOutcome(status="error", error="entity rows lack the 'entity_key' column", elapsed_ms=_elapsed(t0))
    requested = list(typed.entity.features)
    usable: List[str] = []
    for f in requested:
        if f in df.columns:
            numeric = pd.to_numeric(df[f], errors="coerce")
            if numeric.notna().mean() >= 0.8:
                usable.append(f)
    guards = [cardinality(int(len(df)), int(typed.entity.row_cap)), feature_count(usable, requested)]
    if spec.name in ("driver_analysis", "regression", "classification"):
        target_numeric = "target" in df.columns and pd.to_numeric(df["target"], errors="coerce").notna().mean() >= 0.8
        if not target_numeric:
            guards.append(GuardResult(name="target_numeric", passed=False, overridable=False,
                                      detail=f"{typed.entity.target} is not a numeric target column"))
        elif spec.name == "classification":
            distinct = int(pd.to_numeric(df["target"], errors="coerce").dropna().nunique())
            if distinct != 2:
                guards.append(GuardResult(name="target_binary", passed=False, overridable=False,
                                          detail=f"{typed.entity.target} must have exactly two values for classification; found {distinct}"))
    refused = failed(guards)
    if refused and not override_guards:
        return RunOutcome(status="guard_failed", guard_results=guards, elapsed_ms=_elapsed(t0))
    if any(not g.passed and not g.overridable for g in guards):
        return RunOutcome(status="guard_failed", guard_results=guards, elapsed_ms=_elapsed(t0))
    ctx = make_ctx(base_low_confidence or bool(refused))
    engine = importlib.import_module(spec.engine)
    envelope = engine.run(typed, df, ctx, guard_results=guards, features=usable)
    return RunOutcome(status="ok", envelope=envelope, elapsed_ms=_elapsed(t0))


# ── Runner protocol + implementations ────────────────────────────────────────


class AnalysisRunner(Protocol):
    name: str

    async def run(
        self,
        skill: str,
        params: Dict[str, Any],
        series: Dict[str, Any],
        *,
        override_guards: bool = False,
        context: Optional[Dict[str, Any]] = None,
    ) -> RunOutcome: ...


class InProcessRunner:
    """Runs the engine on the API's own event-loop thread pool. Tests only."""

    name = "in_process"

    async def run(self, skill, params, series, *, override_guards=False, context=None) -> RunOutcome:
        ctx = dict(context or {})
        ctx.setdefault("runner", self.name)
        return await asyncio.to_thread(
            execute_skill, skill, params, series, override_guards=override_guards, context=ctx
        )


class LocalSubprocessRunner:
    """Development runner: one child process per run, wall-clock and memory limited.

    The child is ``python -m src.analysis.worker``; it reads one JSON request on
    stdin and writes one JSON outcome on stdout. Nothing the model produced is
    ever passed as code — only the validated ``{skill, params, series}``.
    """

    name = "local_subprocess"

    def __init__(self, *, timeout_seconds: int = LOCAL_TIMEOUT_SECONDS, memory_mb: int = DEFAULT_MEMORY_MB,
                 python: Optional[str] = None, cwd: Optional[str] = None):
        self.timeout_seconds = int(timeout_seconds)
        self.memory_mb = int(memory_mb)
        self.python = python or sys.executable
        self.cwd = cwd or os.getcwd()

    async def run(self, skill, params, series, *, override_guards=False, context=None) -> RunOutcome:
        ctx = dict(context or {})
        ctx.setdefault("runner", self.name)
        request = json.dumps({
            "skill": skill, "params": params, "series": series,
            "override_guards": bool(override_guards), "context": ctx,
            "memory_mb": self.memory_mb,
        }, default=str).encode("utf-8")
        env = {
            # Deliberately minimal: no secrets, no DB coordinates.
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": self.cwd,
            "HOME": os.environ.get("HOME", "/tmp"),
            "NUMBA_CACHE_DIR": os.environ.get("NUMBA_CACHE_DIR", os.path.join(os.environ.get("HOME", "/tmp"), ".numba_cache")),
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        }
        proc = await asyncio.create_subprocess_exec(
            self.python, "-m", "src.analysis.worker",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd, env=env,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(request), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return RunOutcome(status="error", error=f"analysis timed out after {self.timeout_seconds}s")
        if proc.returncode != 0 and not out.strip():
            tail = err.decode("utf-8", "ignore").strip().splitlines()[-1:] or ["no output"]
            return RunOutcome(status="error", error=f"analysis worker exited {proc.returncode}: {tail[0][:300]}")
        try:
            return RunOutcome.from_json(json.loads(out.decode("utf-8")))
        except Exception as exc:  # noqa: BLE001
            return RunOutcome(status="error", error=f"analysis worker returned unreadable output: {exc}")


def build_runner(settings: Any) -> Optional[AnalysisRunner]:
    """Pick the runner from settings. Returns None (skills disabled) when the
    configured runner is not allowed in this deployment mode."""
    mode = str(getattr(settings, "ANALYSIS_RUNNER", "local") or "local").strip().lower()
    dev = bool(getattr(settings, "JEEN_DEV_MODE", True))
    if mode == "sandbox":
        from src.analysis.sandbox_client import HttpSandboxRunner  # noqa: PLC0415

        return HttpSandboxRunner.from_settings(settings)
    if mode in ("local", "local_subprocess"):
        if not dev:
            logger.error(
                "ANALYSIS_RUNNER=local is not allowed when JEEN_DEV_MODE=false; "
                "set ANALYSIS_RUNNER=sandbox. ML skills are disabled."
            )
            return None
        return LocalSubprocessRunner(
            timeout_seconds=int(getattr(settings, "ANALYSIS_TIMEOUT_SECONDS", LOCAL_TIMEOUT_SECONDS) or LOCAL_TIMEOUT_SECONDS),
            memory_mb=int(getattr(settings, "ANALYSIS_MEMORY_MB", DEFAULT_MEMORY_MB) or DEFAULT_MEMORY_MB),
        )
    if mode == "in_process":
        if not dev:
            logger.error("ANALYSIS_RUNNER=in_process is dev/test only. ML skills are disabled.")
            return None
        return InProcessRunner()
    logger.error("Unknown ANALYSIS_RUNNER=%r. ML skills are disabled.", mode)
    return None
