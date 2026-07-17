"""Native provider adapters (server-owned endpoints, scopes, action schemas)."""

from typing import Dict, Optional

from .base import ProviderAdapter, TokenResult
from .graph_mail import GraphMailAdapter

_ADAPTERS: Dict[str, ProviderAdapter] = {
    GraphMailAdapter.provider_id: GraphMailAdapter(),
}


def get_provider(provider_id: str) -> Optional[ProviderAdapter]:
    return _ADAPTERS.get(provider_id)


__all__ = ["ProviderAdapter", "TokenResult", "GraphMailAdapter", "get_provider"]
