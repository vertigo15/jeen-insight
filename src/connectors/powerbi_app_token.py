"""TEMPORARY: app-only (service-principal) Power BI token provider.

Mints Power BI access tokens via the OAuth2 **client-credentials** flow using a
dedicated Azure App Registration that has been granted Power BI access (member of
the workspace + tenant "service principals may use Power BI APIs" enabled). This
is the temporary auth path used until the delegated-OAuth connector subsystem is
provisioned (see ``src/connectors/powerbi_token.py`` for the real, per-user path).

Why app-only here:
  * self-refreshing (MSAL caches the token in-memory and re-acquires near expiry)
  * no user interaction, no ``az`` dependency, no hourly manual refresh

The client secret lives only in ``.env`` (gitignored); it is never committed. The
token never enters LangGraph state — it is resolved per request in the execution
node and passed straight to ``PowerBiDaxClient``.

REMOVE this module together with the rest of the temporary Power BI bridge once
delegated OAuth via connectors is live.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Optional

import msal

from src.config import settings

logger = logging.getLogger(__name__)

# App-only tokens use the resource-wide ``.default`` scope; per-API delegated
# scopes are not applicable to the client-credentials flow.
_SCOPE = "https://analysis.windows.net/powerbi/api/.default"


class PowerBiAppTokenError(RuntimeError):
    """Raised when an app-only Power BI token cannot be acquired."""


def _resolve_tenant() -> str:
    """Tenant for the app-only authority (explicit setting → env → connectors)."""
    return (
        (settings.POWERBI_APP_TENANT_ID or "").strip()
        or (os.getenv("AZURE_AD_TENANT_ID") or "").strip()
        or (settings.CONNECTORS_TENANT_ID or "").strip()
    )


class PowerBiAppTokenProvider:
    """Caches one MSAL confidential app and hands out app-only access tokens.

    MSAL's ``acquire_token_for_client`` maintains its own in-memory token cache
    and only calls the token endpoint when the cached token is missing/expired,
    so repeated calls are cheap. The blocking MSAL call is offloaded to a thread
    so it never stalls the event loop.
    """

    def __init__(self, *, tenant_id: str, client_id: str, client_secret: str) -> None:
        if not (tenant_id and client_id and client_secret):
            raise PowerBiAppTokenError(
                "Power BI app-only credentials are incomplete (need tenant, client id, secret)."
            )
        self._authority = f"https://login.microsoftonline.com/{tenant_id}"
        self._client_id = client_id
        self._client_secret = client_secret
        self._app: Optional[msal.ConfidentialClientApplication] = None
        self._lock = threading.Lock()

    def _get_app(self) -> msal.ConfidentialClientApplication:
        if self._app is None:
            with self._lock:
                if self._app is None:
                    self._app = msal.ConfidentialClientApplication(
                        self._client_id,
                        authority=self._authority,
                        client_credential=self._client_secret,
                    )
        return self._app

    def _acquire(self, force_refresh: bool) -> str:
        app = self._get_app()
        result = None
        if not force_refresh:
            # Serve from MSAL's cache when possible (account=None for app tokens).
            result = app.acquire_token_silent([_SCOPE], account=None)
        if not result or "access_token" not in result:
            result = app.acquire_token_for_client(scopes=[_SCOPE])
        if "access_token" not in result:
            err = result.get("error") if isinstance(result, dict) else "unknown"
            desc = result.get("error_description") if isinstance(result, dict) else ""
            raise PowerBiAppTokenError(
                f"Power BI app-only token acquisition failed: {err} — {str(desc)[:200]}"
            )
        return result["access_token"]

    async def get_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid app-only access token (cached until near expiry)."""
        return await asyncio.to_thread(self._acquire, force_refresh)


_provider_singleton: Optional[PowerBiAppTokenProvider] = None
_singleton_lock = threading.Lock()


def get_app_token_provider() -> Optional[PowerBiAppTokenProvider]:
    """Return a cached provider when app-only creds are configured, else None."""
    global _provider_singleton
    client_id = (settings.POWERBI_APP_CLIENT_ID or "").strip()
    client_secret = (settings.POWERBI_APP_CLIENT_SECRET or "").strip()
    if not (client_id and client_secret):
        return None
    tenant_id = _resolve_tenant()
    if not tenant_id:
        logger.warning(
            "POWERBI_APP_CLIENT_ID/SECRET set but no tenant could be resolved; "
            "app-only Power BI auth is disabled."
        )
        return None
    if _provider_singleton is None:
        with _singleton_lock:
            if _provider_singleton is None:
                _provider_singleton = PowerBiAppTokenProvider(
                    tenant_id=tenant_id,
                    client_id=client_id,
                    client_secret=client_secret,
                )
                logger.info(
                    "✅ Power BI app-only token provider ready (client_id=%s… tenant=%s…) "
                    "— TEMPORARY service-principal auth",
                    client_id[:8], tenant_id[:8],
                )
    return _provider_singleton


__all__ = [
    "PowerBiAppTokenProvider",
    "PowerBiAppTokenError",
    "get_app_token_provider",
]
