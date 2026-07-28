"""Microsoft Power BI adapter — delegated OAuth for read-only dataset access.

Power BI is a **data source**, not an outbound-action connector: this adapter
exists only to obtain and refresh the signed-in user's delegated access token
for the Power BI REST ``executeQueries`` endpoint. Query execution itself lives
in :class:`src.connectors.powerbi.PowerBiDaxClient`; there are no outbound
actions, so :meth:`execute` is intentionally unsupported.

OAuth mirrors :mod:`src.connectors.providers.graph_mail`: authorization-code +
PKCE against Entra ID, ``offline_access`` for a refresh token, and id-token
claim validation (aud/tid/iss/nonce/exp/oid) before an account is bound to the
signed-in identity. The delegated Power BI scope
(``https://analysis.windows.net/powerbi/api/Dataset.Read.All``) is necessary but
not sufficient — the user still needs workspace access, dataset **Read + Build**,
and the tenant setting *"Dataset Execute Queries REST API"* enabled.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

from src.connectors import oauth
from src.connectors.providers.base import ProviderAdapter, TokenResult

logger = logging.getLogger(__name__)

POWERBI_ORIGIN = "https://api.powerbi.com"
# Delegated Power BI service resource. Dataset.Read.All is requested via the
# catalog scopes; ``.default`` could be used instead for a pre-consented app.
POWERBI_RESOURCE = "https://analysis.windows.net/powerbi/api"


class PowerBiAdapter(ProviderAdapter):
    provider_id = "power_bi"
    auth_kind = "oauth"
    allowed_origins = (POWERBI_ORIGIN,)

    # ── config helpers (Entra app) ───────────────────────────────────────────
    def _tenant(self, config: Dict[str, Any]) -> str:
        tenant = (
            (config.get("tenant_id") or "").strip()
            or (os.getenv("CONNECTORS_TENANT_ID") or "").strip()
            or (os.getenv("AZURE_AD_TENANT_ID") or "").strip()
        )
        if not tenant:
            raise ValueError("Power BI connector requires a tenant_id")
        return tenant

    def _client_id(self, config: Dict[str, Any]) -> str:
        client_id = (config.get("client_id") or "").strip() or (
            os.getenv("AZURE_AD_CLIENT_ID") or ""
        ).strip()
        if not client_id:
            raise ValueError("Power BI connector requires a client_id")
        return client_id

    def _authorize_endpoint(self, config: Dict[str, Any]) -> str:
        return f"https://login.microsoftonline.com/{self._tenant(config)}/oauth2/v2.0/authorize"

    def _token_endpoint(self, config: Dict[str, Any]) -> str:
        return f"https://login.microsoftonline.com/{self._tenant(config)}/oauth2/v2.0/token"

    # ── OAuth ─────────────────────────────────────────────────────────────────
    def authorize_url(
        self, *, config, manifest, redirect_uri, state, code_challenge, nonce
    ) -> str:
        return oauth.build_authorize_url(
            authorize_endpoint=self._authorize_endpoint(config),
            client_id=self._client_id(config),
            redirect_uri=redirect_uri,
            scopes=manifest.get("scopes", []),
            state=state,
            code_challenge=code_challenge,
            extra={"prompt": "select_account", "nonce": nonce},
        )

    async def exchange_code(
        self, *, config, manifest, client_secret, code, redirect_uri, code_verifier
    ) -> TokenResult:
        tok = await oauth.exchange_code(
            token_endpoint=self._token_endpoint(config),
            client_id=self._client_id(config),
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
            scopes=manifest.get("scopes"),
        )
        return self._to_result(tok)

    async def refresh(self, *, config, manifest, client_secret, refresh_token) -> TokenResult:
        tok = await oauth.refresh_access_token(
            token_endpoint=self._token_endpoint(config),
            client_id=self._client_id(config),
            client_secret=client_secret,
            refresh_token=refresh_token,
            scopes=manifest.get("scopes"),
        )
        return self._to_result(tok)

    def _to_result(self, tok: Dict[str, Any]) -> TokenResult:
        claims = (
            oauth.decode_id_token_claims(tok.get("id_token", ""))
            if tok.get("id_token")
            else {}
        )
        return TokenResult(
            access_token=tok.get("access_token", ""),
            refresh_token=tok.get("refresh_token"),
            expires_in=tok.get("expires_in"),
            scope=tok.get("scope"),
            id_token=tok.get("id_token"),
            claims=claims,
        )

    def bound_account(self, token: TokenResult) -> Dict[str, str]:
        c = token.claims or {}
        return {
            "tenant_id": str(c.get("tid") or ""),
            "object_id": str(c.get("oid") or ""),
            "upn": str(
                c.get("preferred_username") or c.get("upn") or c.get("email") or ""
            ),
        }

    def validate_and_bind(
        self, token, *, config, expected_nonce, expected_tenant, expected_object_id
    ) -> Dict[str, str]:
        """Verify Entra id_token claims before binding the Power BI account.

        The id_token is fetched directly from the Microsoft token endpoint over
        TLS in the confidential-client code exchange, so its claim set
        (aud/tid/iss/nonce/exp/oid) is enforced here. Any deviation fails closed.
        """
        claims = token.claims or {}
        if not token.id_token or not claims:
            raise ValueError("Provider did not return a verifiable id_token")

        if str(claims.get("aud") or "") != self._client_id(config):
            raise ValueError("id_token audience mismatch")

        tid = str(claims.get("tid") or "")
        if not tid:
            raise ValueError("id_token has no tenant claim")
        if expected_tenant and tid != expected_tenant:
            raise ValueError("id_token tenant mismatch")

        iss = str(claims.get("iss") or "")
        if f"/{tid}/" not in iss or "login.microsoftonline.com" not in iss:
            raise ValueError("id_token issuer mismatch")

        if not expected_nonce or str(claims.get("nonce") or "") != expected_nonce:
            raise ValueError("id_token nonce mismatch")

        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or exp < (time.time() - 60):
            raise ValueError("id_token expired")

        oid = str(claims.get("oid") or "")
        if not oid:
            raise ValueError("id_token has no object id")
        if expected_object_id and oid != expected_object_id:
            raise ValueError(
                "The connected Power BI account does not match your signed-in identity"
            )

        return {
            "tenant_id": tid,
            "object_id": oid,
            "upn": str(
                claims.get("preferred_username")
                or claims.get("upn")
                or claims.get("email")
                or ""
            ),
        }

    # ── Actions ────────────────────────────────────────────────────────────────
    async def execute(
        self, *, action, params, snapshot_payload, config, access_token=None, api_key=None
    ) -> Dict[str, Any]:
        raise ValueError(
            "Power BI is a read-only data source and has no outbound actions. "
            "DAX queries are executed via PowerBiDaxClient, not the action gate."
        )


__all__ = ["PowerBiAdapter", "POWERBI_ORIGIN", "POWERBI_RESOURCE"]
