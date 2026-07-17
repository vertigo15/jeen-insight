"""Static, opinionated catalog of native connector providers (v1).

v1 supports only fixed native provider adapters whose endpoints, OAuth scopes
and action schemas are owned by the server (see ``src/connectors/providers``).
There is deliberately no support for arbitrary remote MCP servers, arbitrary
URLs, or admin-supplied manifests — those are deferred to a later phase behind a
separate security boundary (SSRF/egress allowlist, issuer validation, tool
vetting).

Admins pick a catalog entry, provide the OAuth client secret, optionally set
config (e.g. recipient domain allowlist), and assign Entra groups. The connector
manifest persisted in ``connector_versions`` is derived from the entry here plus
that admin config, and is then immutable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ActionSpec:
    name: str
    label: str
    description: str
    # JSON-schema-ish description of params the SERVER collects/validates.
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CatalogEntry:
    key: str
    provider: str          # native adapter id (maps to providers/*)
    display_name: str
    description: str
    category: str          # 'email' | 'chat' | 'docs' | 'issues' ...
    auth_type: str         # 'oauth2_pkce'
    scopes: List[str]
    actions: List[ActionSpec]
    # Admin-settable config keys with defaults (rendered by the connect wizard).
    config_defaults: Dict[str, Any] = field(default_factory=dict)
    docs_url: Optional[str] = None
    coming_soon: bool = False

    def to_public(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "provider": self.provider,
            "display_name": self.display_name,
            "description": self.description,
            "category": self.category,
            "auth_type": self.auth_type,
            "scopes": list(self.scopes),
            "actions": [
                {"name": a.name, "label": a.label, "description": a.description, "params": a.params}
                for a in self.actions
            ],
            "config_defaults": dict(self.config_defaults),
            "docs_url": self.docs_url,
            "coming_soon": self.coming_soon,
        }

    def build_manifest(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Server-owned manifest snapshot persisted (immutably) on a version."""
        return {
            "key": self.key,
            "provider": self.provider,
            "auth_type": self.auth_type,
            "scopes": list(self.scopes),
            "actions": [
                {"name": a.name, "params": a.params} for a in self.actions
            ],
            "config": {**self.config_defaults, **(config or {})},
        }


# ── v1 catalog ────────────────────────────────────────────────────────────────

_SEND_EMAIL = ActionSpec(
    name="send_email",
    label="Send email",
    description="Send the current result as an email from your own mailbox.",
    params={
        "recipients": {"type": "array", "items": "email", "required": True, "max": 25},
        "subject": {"type": "string", "required": True, "max": 200},
        "note": {"type": "string", "required": False, "max": 2000},
    },
)

CATALOG: Dict[str, CatalogEntry] = {
    "microsoft-graph-mail": CatalogEntry(
        key="microsoft-graph-mail",
        provider="microsoft_graph",
        display_name="Microsoft 365 Mail",
        description=(
            "Send analysis results by email as yourself, from your own Microsoft "
            "365 mailbox (delegated Mail.Send)."
        ),
        category="email",
        auth_type="oauth2_pkce",
        scopes=["openid", "profile", "email", "offline_access", "Mail.Send"],
        actions=[_SEND_EMAIL],
        config_defaults={
            "recipient_domain_allowlist": [],   # empty => sender's own domain only
            "allow_external_recipients": False,
        },
        docs_url="https://learn.microsoft.com/graph/api/user-sendmail",
    ),
    # ── Roadmap (surfaced as "coming soon"; not connectable in v1) ──────────
    "slack-message": CatalogEntry(
        key="slack-message",
        provider="slack",
        display_name="Slack",
        description="Post results to a Slack channel or DM as yourself.",
        category="chat",
        auth_type="oauth2_pkce",
        scopes=["chat:write"],
        actions=[],
        coming_soon=True,
    ),
    "google-sheets": CatalogEntry(
        key="google-sheets",
        provider="google",
        display_name="Google Sheets",
        description="Export results into a Google Sheet you own.",
        category="docs",
        auth_type="oauth2_pkce",
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
        actions=[],
        coming_soon=True,
    ),
    "jira-issue": CatalogEntry(
        key="jira-issue",
        provider="jira",
        display_name="Jira",
        description="Create a Jira issue from an insight.",
        category="issues",
        auth_type="oauth2_pkce",
        scopes=["write:jira-work"],
        actions=[],
        coming_soon=True,
    ),
}


def list_catalog() -> List[CatalogEntry]:
    return list(CATALOG.values())


def get_catalog_entry(key: str) -> Optional[CatalogEntry]:
    return CATALOG.get(key)
