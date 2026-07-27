"""Shared FastAPI dependencies.

These helpers translate the lifecycle state in `src.api.state` into 503/404
HTTP errors at the boundary, so route handlers can remain free of plumbing.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from fastapi import Depends, HTTPException, Request

from src.agent import (
    AgentRegistry,
    DaxAgentRegistry,
    DaxInsightsAgent,
    JeenInsightsAgent,
)
from src.security.internal_auth import Principal
from src.agent.conversation_history import ConversationHistoryService
from src.api import state
from src.connections import (
    ConnectionNotFound,
    ConnectionService,
    UnsupportedConnectionType,
)
from src.agent.prompt_cache import PromptCache
from src.metadata import MetadataLoader


def _require(service: object, name: str) -> object:
    if service is None:
        raise HTTPException(
            status_code=503,
            detail=f"{name} not initialised (app is starting up or shutting down)",
        )
    return service


def get_agent_registry() -> AgentRegistry:
    return _require(state.agent_registry, "AgentRegistry")  # type: ignore[return-value]


def get_dax_agent_registry() -> DaxAgentRegistry:
    return _require(state.dax_agent_registry, "DaxAgentRegistry")  # type: ignore[return-value]


def get_metadata_loader() -> MetadataLoader:
    return _require(state.metadata_loader, "MetadataLoader")  # type: ignore[return-value]


async def get_catalog_provider(source_key: Optional[str] = None) -> object:
    """Return the active catalog data provider for autocomplete/picker data.

    When the global ``catalog_source`` setting is ``mcp`` (and the MCP services
    are wired), returns the ``McpCatalogClient``; otherwise the DB-backed
    ``MetadataLoader``. Both expose ``load_tables_rich``, ``load_columns``,
    ``load_knowledge_questions`` and ``load_all`` with identical signatures and
    return shapes, so callers can use either transparently. Falls back to the
    DB loader on any error.
    """
    if state.mcp_server_service and state.mcp_catalog_client:
        try:
            if await state.mcp_server_service.get_catalog_source(source_key) == "mcp":
                return state.mcp_catalog_client
        except Exception:  # noqa: BLE001
            pass
    return get_metadata_loader()


def get_connection_service() -> ConnectionService:
    return _require(state.connection_service, "ConnectionService")  # type: ignore[return-value]


def get_history_service() -> ConversationHistoryService:
    return _require(state.history_service, "History service")  # type: ignore[return-value]


def get_prompt_cache() -> PromptCache:
    return _require(state.prompt_cache, "PromptCache")  # type: ignore[return-value]


def require_user_id(value: Any) -> str:
    """Return a non-empty authenticated user id or fail closed.

    Browser requests should reach FastAPI through Flask, which stamps this value
    from the signed UI session. Direct callers that omit it cannot safely touch
    user-owned data.
    """
    user_id = str(value or "").strip()
    if not user_id or user_id == "default":
        raise HTTPException(status_code=401, detail="Authenticated user is required")
    return user_id


def require_user_context_user_id(user_context: Optional[Mapping[str, Any]]) -> str:
    return require_user_id((user_context or {}).get("user_id"))


# ── Internal Principal (verified by InternalAuthMiddleware) ─────────────────

def get_principal(request: Request) -> Principal:
    """Return the verified server-side Principal or fail closed with 401.

    The Principal is attached by ``InternalAuthMiddleware`` from the Flask-minted
    internal token. All identity/role/group facts come from here — never from the
    request body or query string.
    """
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return principal  # type: ignore[return-value]


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Admin role required")
    return principal


async def require_connectors_enabled() -> None:
    """Fail with 404 when the global connector master switch is off."""
    from src.security.app_flags import get_connectors_enabled

    if not await get_connectors_enabled():
        raise HTTPException(status_code=404, detail="Connectors feature is disabled")


# ── Connector service getters ───────────────────────────────────────────────

def get_identity_service():
    return _require(state.identity_service, "IdentityService")


def get_registry_service():
    return _require(state.registry_service, "ConnectorRegistryService")


def get_grant_service():
    return _require(state.grant_service, "GrantService")


def get_snapshot_service():
    return _require(state.snapshot_service, "SnapshotService")


def get_audit_service():
    return _require(state.audit_service, "AuditService")


def get_tool_result_service():
    return _require(state.tool_result_service, "ToolResultService")


def get_action_gate():
    return _require(state.action_gate, "ActionGate")


def _configured_tenant() -> str:
    from src.config import settings

    return (
        (settings.CONNECTORS_TENANT_ID or "").strip()
        or __import__("os").getenv("AZURE_AD_TENANT_ID", "").strip()
    )


def resolve_tenant_id(principal: Principal) -> str:
    """Single-tenant isolation: the principal's tenant must match the deployment.

    In a single-tenant deployment every connector identity/group belongs to one
    Entra tenant. A principal presenting a different tenant is rejected (403) so a
    foreign-tenant token can never be bound to a local identity or entitlement.
    """
    configured = _configured_tenant()
    ptid = (principal.tenant_id or "").strip()
    if configured and ptid and ptid != configured:
        raise HTTPException(
            status_code=403,
            detail="Your Microsoft tenant is not authorized for this deployment.",
        )
    return ptid or configured


# Shared app-only Graph client for authoritative membership revalidation. Cheap
# to construct; token is cached internally.
_graph_directory = None


def _get_graph_directory():
    global _graph_directory
    if _graph_directory is None:
        from src.connectors.graph_directory import GraphDirectoryClient

        _graph_directory = GraphDirectoryClient()
    return _graph_directory


async def ensure_identity(principal: Principal):
    """Upsert the caller's canonical identity + refresh group membership.

    Requires an Entra (SSO) principal — per-user connectors are Entra-bound.
    Returns the identity dict.

    Membership freshness: if we already hold *authoritative* (Graph-sourced) and
    still-fresh membership we keep it; otherwise we refresh the cache from the
    token's group claims stamped with the interactive login time (so the TTL is
    measured from login, not from this request) and then best-effort revalidate
    against Graph. This bounds how long a removed group keeps granting access.
    """
    if not principal.is_entra:
        raise HTTPException(
            status_code=403,
            detail="Sign in with Microsoft to use connectors (Entra identity required).",
        )
    identities = get_identity_service()
    tenant_id = resolve_tenant_id(principal)
    try:
        auth_user_id = int(principal.user_id)
    except (TypeError, ValueError):
        auth_user_id = None
    identity = await identities.upsert_identity(
        tenant_id=tenant_id,
        object_id=principal.object_id,
        upn=principal.email,
        display_name=principal.name,
        auth_user_id=auth_user_id,
    )

    current = await identities.get_membership(identity["id"])
    if not (current.get("source") == "graph" and current.get("fresh")):
        from datetime import datetime, timezone

        login_at = (
            datetime.fromtimestamp(principal.auth_time, tz=timezone.utc)
            if principal.auth_time
            else None
        )
        await identities.sync_memberships(
            identity["id"],
            [{"object_id": g, "display_name": None} for g in principal.groups],
            complete=principal.groups_complete,
            source="token",
            synced_at=login_at,
        )
        await identities.maybe_refresh_from_graph(identity, _get_graph_directory())
    return identity


async def resolve_agent(
    source_key: Optional[str],
) -> "JeenInsightsAgent | DaxInsightsAgent":
    """Resolve the per-connection agent or raise the appropriate HTTPException.

    Power BI (``service_type='powerbi'``) connections are dispatched to the
    text-to-DAX ``DaxAgentRegistry`` **before** the SQL ``AgentRegistry`` (whose
    ``get_runner`` would otherwise 501). The returned ``DaxInsightsAgent``
    duck-types ``.sql_runner`` and ``.llm`` so every shared route works
    unchanged. Everything else follows the untouched SQL path.

    - 400 if `source_key` is empty / missing.
    - 404 if the connection isn't registered in `settings_services`.
    - 501 if the connection's `service_type` isn't supported yet.
    - 503 if the registry isn't ready.
    """
    if not source_key:
        raise HTTPException(
            status_code=400,
            detail="Missing 'connection' (source_key). Pick one from /api/connections.",
        )

    # Look up the connection once to decide which engine serves it. This is a
    # cheap indexed lookup; both registries cache their agents afterwards.
    connection_service = get_connection_service()
    try:
        connection = await connection_service.get_connection(source_key)
    except ConnectionNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    try:
        if getattr(connection, "is_power_bi", False):
            return await get_dax_agent_registry().get_agent(source_key)
        return await get_agent_registry().get_agent(source_key)
    except ConnectionNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except UnsupportedConnectionType as e:
        raise HTTPException(status_code=501, detail=str(e)) from e
