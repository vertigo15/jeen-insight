"""Provider adapter interface for native connectors.

Adapters own their OAuth endpoints, scopes and action execution. They never see
the LLM's raw output — recipients/params are collected + validated by the server
action gate and passed in already-validated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TokenResult:
    access_token: str
    refresh_token: Optional[str] = None
    expires_in: Optional[int] = None
    scope: Optional[str] = None
    id_token: Optional[str] = None
    claims: Dict[str, Any] = field(default_factory=dict)


class ProviderAdapter:
    provider_id: str = "base"

    # ── OAuth ──────────────────────────────────────────────────────────────
    def authorize_url(
        self,
        *,
        config: Dict[str, Any],
        manifest: Dict[str, Any],
        redirect_uri: str,
        state: str,
        code_challenge: str,
        nonce: str,
    ) -> str:
        raise NotImplementedError

    async def exchange_code(
        self,
        *,
        config: Dict[str, Any],
        manifest: Dict[str, Any],
        client_secret: Optional[str],
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> TokenResult:
        raise NotImplementedError

    async def refresh(
        self,
        *,
        config: Dict[str, Any],
        manifest: Dict[str, Any],
        client_secret: Optional[str],
        refresh_token: str,
    ) -> TokenResult:
        raise NotImplementedError

    def bound_account(self, token: TokenResult) -> Dict[str, str]:
        """Return {'tenant_id','object_id','upn'} identifying the granted account."""
        raise NotImplementedError

    def validate_and_bind(
        self,
        token: TokenResult,
        *,
        config: Dict[str, Any],
        expected_nonce: str,
        expected_tenant: str,
        expected_object_id: str,
    ) -> Dict[str, str]:
        """Validate id_token claims and return the bound account, or raise.

        Fails closed: providers MUST verify audience, tenant, issuer, nonce,
        expiry and a non-empty subject/object id before an account is bound. The
        default implementation refuses (a provider without id_token validation
        cannot be used for per-user delegated grants).
        """
        raise NotImplementedError

    # ── Actions ────────────────────────────────────────────────────────────
    async def execute(
        self,
        *,
        action: str,
        params: Dict[str, Any],
        snapshot_payload: Dict[str, Any],
        access_token: str,
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        raise NotImplementedError
