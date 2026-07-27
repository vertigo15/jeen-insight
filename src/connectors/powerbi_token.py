"""Per-user Power BI access-token provider (delegated OAuth).

Resolves the signed-in user's canonical identity -> Power BI connector grant ->
a valid delegated access token, refreshing transparently via the provider
adapter when the cached access token is expired (or on a forced 401 replay).

This mirrors ``ActionGate._ensure_access_token`` but is standalone so the
text-to-DAX execution node never depends on the outbound-action gate. The token
is **request-scoped**: it is returned to the caller for a single ``executeQueries``
call and never stored in graph state, trace, or memory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Catalog key of the Power BI connector (see src/connectors/catalog.py).
POWERBI_CONNECTOR_KEY = "power-bi"

# Refresh a little before actual expiry to avoid a race with the API clock.
_EXPIRY_SKEW_SECONDS = 60

# Token-endpoint errors that only the user can clear by re-authorizing. Anything
# else (invalid_client, server_error, transport failures) is a deployment or
# transient problem, and telling the user to reconnect would send them through a
# consent flow that cannot fix it.
_RECONNECT_ERROR_CODES = frozenset({
    "invalid_grant",
    "interaction_required",
    "login_required",
    "consent_required",
})


class PowerBiTokenError(RuntimeError):
    """Raised when a delegated Power BI token cannot be obtained.

    ``needs_connect`` distinguishes the recoverable "user must connect / reconnect
    Power BI" case (surfaced to the UI as a connect prompt) from a hard
    configuration error (connector missing, crypto unavailable, refresh failed).
    """

    def __init__(self, message: str, *, needs_connect: bool = False) -> None:
        super().__init__(message)
        self.needs_connect = needs_connect


@dataclass
class PowerBiToken:
    """A request-scoped delegated token handle (never persisted in graph state)."""

    access_token: str
    expires_at: Optional[datetime]
    external_account: Optional[str]
    grant_id: str
    connector_id: str


class PowerBiTokenProvider:
    """Resolves and refreshes delegated Power BI tokens for a user."""

    def __init__(
        self,
        *,
        identity_service: Any,
        registry_service: Any,
        grant_service: Any,
        connector_key: str = POWERBI_CONNECTOR_KEY,
    ) -> None:
        self.identities = identity_service
        self.registry = registry_service
        self.grants = grant_service
        self.connector_key = connector_key

    async def get_token_for_auth_user(
        self, auth_user_id: Any, *, force_refresh: bool = False
    ) -> PowerBiToken:
        """Return a valid delegated token for the given ``auth_users`` id.

        Raises :class:`PowerBiTokenError` (with ``needs_connect=True`` when the
        user simply hasn't connected Power BI, or reconnection is required).
        """
        if not (self.identities and self.registry and self.grants):
            raise PowerBiTokenError(
                "The connector platform is not available, so Power BI cannot be "
                "queried. Ask an admin to enable connectors and configure Power BI."
            )

        try:
            auth_user_id_int = int(auth_user_id)
        except (TypeError, ValueError):
            raise PowerBiTokenError(
                "Sign in with Microsoft to query Power BI.", needs_connect=True
            )

        identity = await self.identities.get_by_auth_user(auth_user_id_int)
        if not identity:
            raise PowerBiTokenError(
                "Connect your Power BI account to run this query.", needs_connect=True
            )

        connector = await self.registry.get_by_key(self.connector_key)
        if not connector:
            raise PowerBiTokenError(
                "Power BI is not configured on this deployment. Ask an admin to add "
                "the Power BI connector."
            )
        connector_id = connector["id"]
        version = connector.get("current_version") or {}
        manifest: Dict[str, Any] = version.get("manifest") or {}
        config: Dict[str, Any] = (manifest.get("config") or version.get("config") or {})

        if not connector.get("is_enabled"):
            raise PowerBiTokenError(
                "Power BI querying is turned off on this deployment. Ask an admin "
                "to enable the Power BI connector."
            )

        # Entitlement is re-checked on every query, not just at connect time: a
        # grant issued earlier must stop working the moment the user loses group
        # access. Not entitled is never `needs_connect` — reconnecting cannot
        # grant entitlement back.
        allowed, reason = await self.identities.can_use_connector(
            identity["id"],
            connector_id,
            group_grant_ids=connector.get("group_grants") or [],
        )
        if not allowed:
            logger.warning(
                "PowerBiTokenProvider: identity %s not entitled to connector %s (%s)",
                identity["id"], connector_id, reason,
            )
            if reason == "membership_stale":
                # Group membership is refreshed out of band and fails closed while
                # stale; that is transient, so don't send the user to an admin.
                raise PowerBiTokenError(
                    "Your Power BI access could not be verified right now. Please try again."
                )
            raise PowerBiTokenError(
                "Your account is not allowed to query Power BI. Ask an admin for access."
            )

        grant = await self.grants.get_grant(identity["id"], connector_id)
        if not grant or grant.get("status") != "active":
            raise PowerBiTokenError(
                "Connect your Power BI account to run this query.", needs_connect=True
            )
        grant_id = grant["id"]

        access_token = await self._ensure_access_token(
            connector_id=connector_id,
            grant_id=grant_id,
            manifest=manifest,
            config=config,
            force_refresh=force_refresh,
        )

        # Best-effort last-used stamp (never fatal).
        try:
            await self.grants.touch_used(grant_id)
        except Exception:  # noqa: BLE001
            pass

        current = await self.grants.get_access_token(grant_id)
        expires_at = current.get("expires_at") if current else None
        return PowerBiToken(
            access_token=access_token,
            expires_at=expires_at,
            external_account=grant.get("external_account"),
            grant_id=grant_id,
            connector_id=connector_id,
        )

    async def _ensure_access_token(
        self,
        *,
        connector_id: str,
        grant_id: str,
        manifest: Dict[str, Any],
        config: Dict[str, Any],
        force_refresh: bool,
    ) -> str:
        now = datetime.now(timezone.utc)
        if not force_refresh:
            current = await self.grants.get_access_token(grant_id)
            if current and current.get("value"):
                exp = current.get("expires_at")
                if exp is None or exp > now + timedelta(seconds=_EXPIRY_SKEW_SECONDS):
                    return current["value"]

        refresh = await self.grants.get_refresh_token(grant_id)
        if not refresh:
            raise PowerBiTokenError(
                "Your Power BI connection expired. Please reconnect.",
                needs_connect=True,
            )

        from src.connectors.providers import get_provider

        provider = get_provider(manifest.get("provider") or "power_bi")
        if provider is None:
            raise PowerBiTokenError("Power BI provider adapter is missing.")

        client_secret = await self.registry.get_client_secret(connector_id)
        try:
            token = await provider.refresh(
                config=config,
                manifest=manifest,
                client_secret=client_secret,
                refresh_token=refresh,
            )
        except Exception as exc:  # noqa: BLE001
            raise self._refresh_error(exc) from exc

        expires_at: Optional[datetime] = None
        if token.expires_in:
            expires_at = now + timedelta(seconds=int(token.expires_in))
        await self.grants.store_refreshed_tokens(
            grant_id,
            access_token=token.access_token,
            access_expires_at=expires_at,
            refresh_token=getattr(token, "refresh_token", None),
        )
        return token.access_token

    @staticmethod
    def _refresh_error(exc: Exception) -> PowerBiTokenError:
        """Map a refresh failure to the right user-facing outcome.

        Only a provider error the user can clear by re-consenting becomes
        ``needs_connect``; misconfiguration and transient failures must not push
        the user into a connect flow that will fail the same way.
        """
        code = str(getattr(exc, "error_code", "") or "").lower()
        if code in _RECONNECT_ERROR_CODES:
            logger.info("PowerBiTokenProvider: refresh needs reconnect (%s)", code)
            return PowerBiTokenError(
                "Your Power BI authorization has expired. Please reconnect.",
                needs_connect=True,
            )
        logger.warning(
            "PowerBiTokenProvider: token refresh failed (%s): %s",
            code or type(exc).__name__, exc,
        )
        return PowerBiTokenError(
            "Power BI sign-in is temporarily unavailable. Please try again; if it "
            "keeps failing, ask an admin to check the Power BI connector."
        )


__all__ = [
    "PowerBiToken",
    "PowerBiTokenError",
    "PowerBiTokenProvider",
    "POWERBI_CONNECTOR_KEY",
]
