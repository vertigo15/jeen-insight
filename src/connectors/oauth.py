"""OAuth 2.1 authorization-code + PKCE helper for native providers.

Kept provider-agnostic at the HTTP level; provider adapters supply the authority,
client id/secret, scopes and redirect URI. Uses async httpx for token exchange.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)


def generate_pkce() -> Tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(
    *,
    authorize_endpoint: str,
    client_id: str,
    redirect_uri: str,
    scopes: List[str],
    state: str,
    code_challenge: str,
    extra: Optional[Dict[str, str]] = None,
) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if extra:
        params.update(extra)
    return f"{authorize_endpoint}?{urlencode(params)}"


async def exchange_code(
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: Optional[str],
    code: str,
    redirect_uri: str,
    code_verifier: str,
    scopes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    data = {
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret
    if scopes:
        data["scope"] = " ".join(scopes)
    return await _post_token(token_endpoint, data)


async def refresh_access_token(
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: Optional[str],
    refresh_token: str,
    scopes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if client_secret:
        data["client_secret"] = client_secret
    if scopes:
        data["scope"] = " ".join(scopes)
    return await _post_token(token_endpoint, data)


async def _post_token(token_endpoint: str, data: Dict[str, str]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            token_endpoint,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code != 200:
        # Surface provider error code/description without leaking secrets.
        try:
            body = resp.json()
            err = body.get("error"), body.get("error_description")
        except Exception:  # noqa: BLE001
            err = (str(resp.status_code), resp.text[:200])
        raise OAuthError(f"Token endpoint error: {err[0]} — {err[1]}")
    return resp.json()


class OAuthError(RuntimeError):
    pass


def decode_id_token_claims(id_token: str) -> Dict[str, Any]:
    """Decode (WITHOUT signature verification) the JWT payload for identity binding.

    The id_token is received directly from the trusted token endpoint over TLS in
    the confidential-client code exchange, so we read claims for mailbox binding.
    """
    import json

    try:
        parts = id_token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:  # noqa: BLE001
        return {}
