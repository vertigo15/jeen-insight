"""Tavily provider — web search (API-key, READ tool).

API-key model (plan Phase 2/5):
  * No per-user OAuth. The admin stores an envelope-encrypted API key (distinct
    purpose + AAD) via the registry; ``execute`` receives it as ``api_key``.
  * READ tool: returns DATA (untrusted external content). The gate captures the
    result into an encrypted TTL artifact and the response-only continuation feeds
    it back with tools DISABLED. Nothing is written anywhere.
  * All outbound calls go through the SSRF-hardened egress helper to the single
    fixed ``https://api.tavily.com`` origin.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.connectors import egress
from src.connectors.providers.base import ProviderAdapter, TokenResult

logger = logging.getLogger(__name__)

TAVILY_ORIGIN = "https://api.tavily.com"
TAVILY_SEARCH = f"{TAVILY_ORIGIN}/search"
# Reads can pull more data than a control-plane call; still hard-capped.
_READ_MAX_BYTES = 512 * 1024
_MAX_RESULTS = 10


class TavilyAdapter(ProviderAdapter):
    provider_id = "tavily"
    auth_kind = "api_key"
    allowed_origins = (TAVILY_ORIGIN,)

    # OAuth methods are intentionally unsupported for an api_key provider.
    def authorize_url(self, **_kwargs) -> str:  # pragma: no cover - guarded upstream
        raise ValueError("Tavily is an API-key connector; it has no per-user sign-in")

    def bound_account(self, token: TokenResult) -> Dict[str, str]:  # pragma: no cover
        raise ValueError("Tavily has no per-user account")

    async def execute(self, *, action, params, snapshot_payload, config, access_token=None, api_key=None) -> Dict[str, Any]:
        if action != "web_search":
            raise ValueError(f"Unsupported action for tavily: {action}")
        if not api_key:
            raise ValueError("Tavily requires an API key")
        query = (params.get("query") or "").strip()
        if not query:
            raise ValueError("No query")
        max_results = int(params.get("max_results") or 5)
        max_results = max(1, min(max_results, _MAX_RESULTS))
        body = await egress.request_json(
            "POST", TAVILY_SEARCH, allowed_origins=self.allowed_origins,
            json={
                "api_key": api_key,
                "query": query[:400],
                "max_results": max_results,
                "search_depth": "basic",
            },
            max_bytes=_READ_MAX_BYTES,
        )
        status = int(body.get("_status_code", 0) or 0)
        accepted = status == 200 and isinstance(body.get("results"), list)
        results: List[Dict[str, Any]] = []
        for r in (body.get("results") or [])[:max_results]:
            if isinstance(r, dict):
                results.append({
                    "title": str(r.get("title") or "")[:300],
                    "url": str(r.get("url") or "")[:1000],
                    "content": str(r.get("content") or "")[:2000],
                })
        return {
            "status_code": status,
            "accepted": accepted,
            "provider": "tavily",
            # READ tools carry their data back to the gate for the artifact store.
            "data": {"query": query, "answer": body.get("answer"), "results": results},
            "error": None if accepted else str(body.get("error") or "search_failed"),
        }
