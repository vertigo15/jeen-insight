"""Microsoft Entra ID (Azure AD) OAuth helpers for the Flask UI."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

import msal

SCOPES = ["User.Read"]


def is_configured() -> bool:
    """Return True when all required Entra env vars are set."""
    return all(
        os.getenv(key)
        for key in ("AZURE_AD_CLIENT_ID", "AZURE_AD_CLIENT_SECRET", "AZURE_AD_TENANT_ID")
    )


def _authority() -> str:
    return f"https://login.microsoftonline.com/{os.environ['AZURE_AD_TENANT_ID']}"


def _client() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        os.environ["AZURE_AD_CLIENT_ID"],
        authority=_authority(),
        client_credential=os.environ["AZURE_AD_CLIENT_SECRET"],
    )


def redirect_uri(base_url: str) -> str:
    """OAuth redirect URI — explicit env wins, else derived from public app URL."""
    explicit = os.getenv("AZURE_AD_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    return f"{base_url.rstrip('/')}/auth/microsoft/callback"


def build_auth_url(*, redirect_uri: str, state: str) -> str:
    """Build the Microsoft authorize URL for the authorization-code flow."""
    return _client().get_authorization_request_url(
        scopes=SCOPES,
        redirect_uri=redirect_uri,
        state=state,
    )


def exchange_code(code: str, *, redirect_uri: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Exchange an authorization code for tokens. Returns (result, error_message)."""
    result = _client().acquire_token_by_authorization_code(
        code,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    if "error" in result:
        return None, result.get("error_description") or result.get("error")
    return result, None


def profile_from_token_result(result: Dict[str, Any]) -> Dict[str, str]:
    """Extract email and display name from MSAL token response claims."""
    claims = result.get("id_token_claims") or {}
    email = (
        claims.get("preferred_username")
        or claims.get("email")
        or claims.get("upn")
        or ""
    ).strip().lower()
    name = (claims.get("name") or (email.split("@")[0] if email else "") or "User").strip()
    return {"email": email, "name": name}
