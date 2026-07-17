"""CLI entry point for the NL2SQL golden-set eval.

Examples
--------
Offline (safety + groundedness + greeting routing; no Azure needed)::

    python -m evals.run_eval

Live (also scores LLM routing via the real fused_router)::

    python -m evals.run_eval --live

Fail the process when a dimension drops below a threshold (for CI)::

    python -m evals.run_eval --min-safety 1.0 --min-groundedness 1.0

The offline scorers reuse the production guardrails, so a failure here means a
real regression in the safety/validation stack — not eval drift.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from evals.harness import EvalReport, evaluate, load_golden_set

logger = logging.getLogger(__name__)

_DEFAULT_DATASET = Path(__file__).parent / "datasets" / "golden_set.yaml"


async def _build_live_classifier(dataset: Dict[str, Any]):
    """Construct a route classifier backed by the real ``fused_router`` node.

    Best-effort: requires Azure OpenAI env vars (AZURE_OPENAI_*). Returns None
    if the LLM service can't be built, so the caller can fall back to skipping
    LLM-dependent route cases.
    """
    try:
        from src.agent.langgraph_agent.nodes.router import make_fused_router
        from src.agent.langgraph_agent.prompt_loader import PromptLoader
        from src.agent.llm_service import LangChainLlmService
        from src.config import settings

        router_deployment = (settings.AZURE_OPENAI_ROUTER_DEPLOYMENT or "").strip() or None
        llm = LangChainLlmService.from_env_azure(
            pool=None, settings=settings, deployment_override=router_deployment
        )
        prompt_loader = PromptLoader()
        node = make_fused_router(llm, prompt_loader)
    except Exception as exc:  # noqa: BLE001
        logger.warning("live classifier unavailable (%s) — routing cases will be skipped", exc)
        return None

    catalogs = dataset.get("catalogs", {}) or {}

    async def classify(
        question: str,
        *,
        catalog_name: Optional[str] = None,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        catalog = catalogs.get(catalog_name or "", {})
        conv: List[Dict[str, Any]] = []
        for turn in history or []:
            conv.append({
                "natural_language_query": turn.get("q"),
                "generated_sql": turn.get("sql"),
                "result_artifact": turn.get("artifact"),
                "row_count": (turn.get("artifact") or {}).get("row_count"),
            })
        state = {
            "question": question,
            "conversation_history": conv,
            "connection_display_name": (catalog or {}).get("display_name") or "the database",
            "llm_timeout_seconds": settings.LLM_TIMEOUT_SECONDS,
        }
        result = await node(state)
        return str(result.get("route", "needs_query"))

    return classify


def _threshold_failures(report: EvalReport, thresholds: Dict[str, float]) -> List[str]:
    problems: List[str] = []
    for dim, minimum in thresholds.items():
        if minimum is None:
            continue
        score = report.dimension_score(dim)
        if score.scored == 0:
            continue  # nothing scored (e.g. offline routing) → don't gate
        if score.accuracy < minimum:
            problems.append(
                f"{dim} accuracy {score.accuracy * 100:.1f}% < required {minimum * 100:.1f}%"
            )
    return problems


async def _run(args: argparse.Namespace) -> int:
    dataset = load_golden_set(args.dataset)

    classifier = None
    if args.live:
        classifier = await _build_live_classifier(dataset)

    report = await evaluate(dataset, route_classifier=classifier)

    if args.json:
        payload = {
            "dimensions": {
                dim: vars(report.dimension_score(dim)) for dim in report.dimensions
            },
            "failures": [vars(r) for r in report.failures()],
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(report.summary())

    thresholds = {
        "safety": args.min_safety,
        "groundedness": args.min_groundedness,
        "route": args.min_route,
    }
    problems = _threshold_failures(report, thresholds)
    if problems:
        print("\nTHRESHOLD FAILURES:")
        for p in problems:
            print(f"  - {p}")
        return 1
    # Even without thresholds, non-zero exit if any non-skipped case failed.
    if report.failures():
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the NL2SQL golden-set eval.")
    parser.add_argument("--dataset", default=str(_DEFAULT_DATASET),
                        help="Path to the golden-set YAML (default: bundled set).")
    parser.add_argument("--live", action="store_true",
                        help="Score LLM routing via the real fused_router (needs Azure env).")
    parser.add_argument("--json", action="store_true", help="Emit a JSON report.")
    parser.add_argument("--min-safety", type=float, default=None,
                        help="Fail if safety accuracy < this fraction (e.g. 1.0).")
    parser.add_argument("--min-groundedness", type=float, default=None,
                        help="Fail if groundedness accuracy < this fraction.")
    parser.add_argument("--min-route", type=float, default=None,
                        help="Fail if route accuracy < this fraction.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
