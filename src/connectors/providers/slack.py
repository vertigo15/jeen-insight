"""Slack provider — post a result to a channel as the signed-in user (delegated).

External-identity model (plan Phase 4):
  * The Entra principal remains the authorization subject. The OAuth *consent* is
    bound to that principal by the CSRF-protected oauth-session flow in
    ``me_connections`` (state is tied to the identity that started it), NOT by
    matching Slack ids to Entra tenant/object.
  * ``validate_and_bind`` therefore validates the SLACK side (workspace allowlist)
    and never compares against ``expected_tenant``/``expected_object_id``.
  * All outbound calls go through the SSRF-hardened egress helper to the single
    fixed ``https://slack.com`` origin.
"""

from __future__ import annotations

import html
import logging
import os
from typing import Any, Dict, List, Optional

from src.connectors import egress, oauth
from src.connectors.providers.base import ProviderAdapter, TokenResult

logger = logging.getLogger(__name__)

SLACK_ORIGIN = "https://slack.com"
SLACK_AUTHORIZE = f"{SLACK_ORIGIN}/oauth/v2/authorize"
SLACK_TOKEN = f"{SLACK_ORIGIN}/api/oauth.v2.access"
SLACK_POST = f"{SLACK_ORIGIN}/api/chat.postMessage"
_MAX_ROWS = 20
_MAX_COLS = 8


class SlackAdapter(ProviderAdapter):
    provider_id = "slack"
    auth_kind = "oauth"
    allowed_origins = (SLACK_ORIGIN,)

    def _client_id(self, config: Dict[str, Any]) -> str:
        cid = (config.get("client_id") or "").strip() or (os.getenv("SLACK_CLIENT_ID") or "").strip()
        if not cid:
            raise ValueError("Slack connector requires a client_id")
        return cid

    # ── OAuth ────────────────────────────────────────────────────────────────
    def authorize_url(self, *, config, manifest, redirect_uri, state, code_challenge, nonce) -> str:
        # Slack v2 distinguishes bot scopes (``scope``) from user scopes
        # (``user_scope``). Posting as the member requires a USER token.
        scopes = manifest.get("scopes", [])
        from urllib.parse import urlencode

        params = {
            "client_id": self._client_id(config),
            "user_scope": " ".join(scopes),
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{SLACK_AUTHORIZE}?{urlencode(params)}"

    async def exchange_code(self, *, config, manifest, client_secret, code, redirect_uri, code_verifier) -> TokenResult:
        body = await egress.request_json(
            "POST", SLACK_TOKEN, allowed_origins=self.allowed_origins,
            data={
                "client_id": self._client_id(config),
                "client_secret": client_secret or "",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        return self._to_result(body)

    async def refresh(self, *, config, manifest, client_secret, refresh_token) -> TokenResult:
        # Token rotation is off by default for Slack; user tokens don't expire.
        raise ValueError("Slack tokens do not support refresh; reconnect if revoked")

    def _to_result(self, body: Dict[str, Any]) -> TokenResult:
        if not body.get("ok"):
            raise oauth.OAuthError(f"Slack OAuth error: {body.get('error') or 'unknown'}")
        authed = body.get("authed_user") or {}
        team = body.get("team") or {}
        return TokenResult(
            access_token=str(authed.get("access_token") or ""),
            refresh_token=None,
            expires_in=None,
            scope=str(authed.get("scope") or ""),
            id_token=None,
            claims={
                "team_id": str(team.get("id") or ""),
                "team_name": str(team.get("name") or ""),
                "user_id": str(authed.get("id") or ""),
            },
        )

    def bound_account(self, token: TokenResult) -> Dict[str, str]:
        c = token.claims or {}
        return {
            "tenant_id": str(c.get("team_id") or ""),
            "object_id": str(c.get("user_id") or ""),
            "upn": f"{c.get('user_id') or 'user'}@{c.get('team_name') or 'slack'}",
        }

    def validate_and_bind(self, token, *, config, expected_nonce, expected_tenant, expected_object_id) -> Dict[str, str]:
        # External identity: do NOT compare Slack ids to Entra tenant/object. Bind
        # the workspace and enforce the (optional) workspace allowlist.
        acct = self.bound_account(token)
        if not token.access_token:
            raise ValueError("Slack did not return a user access token")
        allowed_team = (config.get("allowed_team_id") or "").strip()
        if allowed_team and acct["tenant_id"] != allowed_team:
            raise ValueError("Connected Slack workspace is not permitted by policy")
        return acct

    # ── Action execution ─────────────────────────────────────────────────────
    async def execute(self, *, action, params, snapshot_payload, config, access_token=None, api_key=None) -> Dict[str, Any]:
        if action != "post_message":
            raise ValueError(f"Unsupported action for slack: {action}")
        if not access_token:
            raise ValueError("Slack requires a user access token")
        snapshot_payload = snapshot_payload or {}
        channel = params.get("channel")
        if not channel:
            raise ValueError("No channel")
        text = self._render_text(
            note=params.get("note") or "",
            columns=snapshot_payload.get("columns", []),
            rows=snapshot_payload.get("rows", []),
        )
        body = await egress.request_json(
            "POST", SLACK_POST, allowed_origins=self.allowed_origins,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={"channel": channel, "text": text},
        )
        accepted = bool(body.get("ok"))
        return {
            "status_code": body.get("_status_code", 200),
            "accepted": accepted,
            "provider": "slack",
            "error": None if accepted else str(body.get("error") or "post_failed"),
        }

    def _render_text(self, *, note: str, columns: List[Any], rows: List[Any]) -> str:
        col_names = [self._col_name(c) for c in columns[:_MAX_COLS]]
        lines = []
        if note:
            lines.append(note)
        lines.append("```")
        lines.append(" | ".join(col_names))
        for row in rows[:_MAX_ROWS]:
            cells = row[:_MAX_COLS] if isinstance(row, (list, tuple)) else [row]
            lines.append(" | ".join("" if v is None else str(v) for v in cells))
        if len(rows) > _MAX_ROWS:
            lines.append(f"… {len(rows) - _MAX_ROWS} more row(s)")
        lines.append("```")
        lines.append("_Sent from Jeen Insights._")
        # Slack mrkdwn is plain text; escape angle brackets to avoid link parsing.
        return html.escape("\n".join(lines), quote=False)

    @staticmethod
    def _col_name(c: Any) -> str:
        if isinstance(c, dict):
            return str(c.get("name") or c.get("column") or "")
        return str(c)
