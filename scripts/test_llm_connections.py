"""Live smoke test for configured LLM model connections.

Loads every enabled model from the metadata DB (admin_models +
admin_models_providers + admin_providers), builds its LangChain chat model,
and runs a tiny one-token generation against each to confirm the provider
credentials and endpoint actually work.

The probing logic lives in ``src.agent.llm_health`` so this CLI and the
``/api/settings/models/health`` endpoint can never drift apart.

Run inside the API container (it has the deps + .env):

    docker exec -e PYTHONPATH=/app -w /app jeen-insights-api \\
        python scripts/test_llm_connections.py
"""

from __future__ import annotations

import asyncio
import sys

from src.agent.llm_health import PASS, SKIP, probe_all
from src.metadata import get_metadata_pool, close_metadata_pool


async def main() -> int:
    pool = await get_metadata_pool()
    try:
        results = await probe_all(pool)
    finally:
        await close_metadata_pool()

    if not results:
        print("No enabled models found in admin_models / admin_models_providers.")
        return 1

    print(f"Testing {len(results)} enabled model connection(s)\n" + "=" * 64)
    passed = failed = skipped = 0
    for h in results:
        if h.status == PASS:
            passed += 1
        elif h.status == SKIP:
            skipped += 1
        else:
            failed += 1
        default_tag = "  [default]" if h.is_default else ""
        print(f"[{h.status.upper()}] {h.name} ({h.provider}:{h.identifier}){default_tag}")
        print(f"        {h.elapsed_s:5.2f}s  -> {h.detail}")

    print("=" * 64)
    print(
        f"{passed} passed, {failed} failed, {skipped} skipped "
        f"(of {len(results)} enabled model(s))."
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
