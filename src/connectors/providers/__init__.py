"""Native provider adapters (server-owned endpoints, scopes, action schemas)."""

from typing import Dict, Optional

from .base import ProviderAdapter, TokenResult
from .graph_mail import GraphMailAdapter
from .jira import JiraAdapter
from .slack import SlackAdapter
from .tavily import TavilyAdapter

_ADAPTERS: Dict[str, ProviderAdapter] = {
    GraphMailAdapter.provider_id: GraphMailAdapter(),
    SlackAdapter.provider_id: SlackAdapter(),
    JiraAdapter.provider_id: JiraAdapter(),
    TavilyAdapter.provider_id: TavilyAdapter(),
}


def get_provider(provider_id: str) -> Optional[ProviderAdapter]:
    return _ADAPTERS.get(provider_id)


__all__ = [
    "ProviderAdapter",
    "TokenResult",
    "GraphMailAdapter",
    "SlackAdapter",
    "JiraAdapter",
    "TavilyAdapter",
    "get_provider",
]
