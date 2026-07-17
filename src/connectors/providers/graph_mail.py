"""Microsoft Graph mail adapter — send email as the signed-in user (delegated).

Narrowed by design (plan Phase 2):
  - Delegated ``Mail.Send`` + ``offline_access`` only.
  - POST ``/me/sendMail`` with a SERVER-rendered, HTML-escaped summary body.
  - No attachments, no CC/BCC. Recipients are validated upstream by the gate.
  - 202 means *accepted for delivery*, not delivered; we do not retry on an
    unknown outcome (avoids duplicate sends). No SMTP fallback.
  - The granted mailbox identity (tid/oid) is returned for binding to the
    logged-in identity; a mismatch is rejected by the callback unless an explicit
    linking policy exists.
"""

from __future__ import annotations

import html
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from src.connectors import oauth
from src.connectors.providers.base import ProviderAdapter, TokenResult

logger = logging.getLogger(__name__)

GRAPH_SENDMAIL = "https://graph.microsoft.com/v1.0/me/sendMail"
_MAX_ROWS = 50
_MAX_COLS = 20


class GraphMailAdapter(ProviderAdapter):
    provider_id = "microsoft_graph"

    # ── config helpers ──────────────────────────────────────────────────────
    def _tenant(self, config: Dict[str, Any]) -> str:
        tenant = (
            (config.get("tenant_id") or "").strip()
            or (os.getenv("CONNECTORS_TENANT_ID") or "").strip()
            or (os.getenv("AZURE_AD_TENANT_ID") or "").strip()
        )
        if not tenant:
            raise ValueError("Microsoft Graph connector requires a tenant_id")
        return tenant

    def _client_id(self, config: Dict[str, Any]) -> str:
        client_id = (config.get("client_id") or "").strip() or (os.getenv("AZURE_AD_CLIENT_ID") or "").strip()
        if not client_id:
            raise ValueError("Microsoft Graph connector requires a client_id")
        return client_id

    def _authorize_endpoint(self, config: Dict[str, Any]) -> str:
        return f"https://login.microsoftonline.com/{self._tenant(config)}/oauth2/v2.0/authorize"

    def _token_endpoint(self, config: Dict[str, Any]) -> str:
        return f"https://login.microsoftonline.com/{self._tenant(config)}/oauth2/v2.0/token"

    # ── OAuth ────────────────────────────────────────────────────────────────
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
        claims = oauth.decode_id_token_claims(tok.get("id_token", "")) if tok.get("id_token") else {}
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
            "upn": str(c.get("preferred_username") or c.get("upn") or c.get("email") or ""),
        }

    def validate_and_bind(
        self, token, *, config, expected_nonce, expected_tenant, expected_object_id
    ) -> Dict[str, str]:
        """Verify the Entra id_token claims before binding the mailbox.

        The id_token is fetched directly from the Microsoft token endpoint over
        TLS in the confidential-client code exchange, so we treat it as issued by
        Microsoft and enforce the claim set (aud/tid/iss/nonce/exp/oid). Any
        deviation fails closed — no mailbox is bound.
        """
        import time

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
        # Entra v2 issuer: https://login.microsoftonline.com/{tid}/v2.0
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
            raise ValueError("Connected mailbox does not match your signed-in identity")

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

    # ── Action execution ─────────────────────────────────────────────────────
    async def execute(
        self, *, action, params, snapshot_payload, access_token, config
    ) -> Dict[str, Any]:
        if action != "send_email":
            raise ValueError(f"Unsupported action for microsoft_graph: {action}")
        recipients: List[str] = params.get("recipients") or []
        subject: str = params.get("subject") or "(no subject)"
        note: str = params.get("note") or ""
        if not recipients:
            raise ValueError("No recipients")

        body_html = self._render_body_html(
            note=note,
            columns=snapshot_payload.get("columns", []),
            rows=snapshot_payload.get("rows", []),
        )
        message = {
            "message": {
                "subject": subject[:255],
                "body": {"contentType": "HTML", "content": body_html},
                "toRecipients": [
                    {"emailAddress": {"address": r}} for r in recipients
                ],
            },
            "saveToSentItems": True,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                GRAPH_SENDMAIL,
                json=message,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        accepted = resp.status_code == 202
        return {
            "status_code": resp.status_code,
            "accepted": accepted,
            # No message body echoed back; Graph sendMail returns no id.
            "provider": "microsoft_graph",
        }

    # ── server-side HTML rendering (all values escaped) ──────────────────────
    def _render_body_html(self, *, note: str, columns: List[Any], rows: List[Any]) -> str:
        col_names = [self._col_name(c) for c in columns[:_MAX_COLS]]
        header = "".join(f"<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ccc'>{html.escape(c)}</th>" for c in col_names)
        body_rows = []
        for row in rows[:_MAX_ROWS]:
            cells = row[:_MAX_COLS] if isinstance(row, (list, tuple)) else [row]
            tds = "".join(
                f"<td style='padding:4px 8px;border-bottom:1px solid #eee'>{html.escape('' if v is None else str(v))}</td>"
                for v in cells
            )
            body_rows.append(f"<tr>{tds}</tr>")
        note_html = f"<p>{html.escape(note)}</p>" if note else ""
        truncated = ""
        if len(rows) > _MAX_ROWS:
            truncated = f"<p style='color:#666'>Showing first {_MAX_ROWS} of {len(rows)} rows.</p>"
        return (
            "<div style='font-family:Segoe UI,Arial,sans-serif;font-size:14px'>"
            f"{note_html}"
            "<table style='border-collapse:collapse;margin-top:8px'>"
            f"<thead><tr>{header}</tr></thead>"
            f"<tbody>{''.join(body_rows)}</tbody>"
            "</table>"
            f"{truncated}"
            "<p style='color:#999;margin-top:12px'>Sent from Jeen Insights.</p>"
            "</div>"
        )

    @staticmethod
    def _col_name(c: Any) -> str:
        if isinstance(c, dict):
            return str(c.get("name") or c.get("column") or "")
        return str(c)
