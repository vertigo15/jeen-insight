"""Jira provider — create an issue from a result (Atlassian OAuth 2.0 3LO).

External-identity model (plan Phase 4):
  * The Entra principal is the authorization subject; Atlassian consent is bound
    to it by the CSRF-protected oauth-session flow (not by Entra id matching).
  * The target Jira site is a SERVER-OWNED, admin-configured cloud id — never a
    user-supplied base URL. ``validate_and_bind`` verifies the granted token can
    actually reach that cloud id; ``execute`` always calls the FIXED Atlassian API
    host ``https://api.atlassian.com/ex/jira/{cloud_id}/...``.
  * All outbound calls go through the SSRF-hardened egress helper to the two fixed
    Atlassian origins.
"""

from __future__ import annotations

import html
import logging
import os
from typing import Any, Dict, List, Optional

from src.connectors import egress, oauth
from src.connectors.providers.base import ProviderAdapter, TokenResult

logger = logging.getLogger(__name__)

ATLASSIAN_AUTH_ORIGIN = "https://auth.atlassian.com"
ATLASSIAN_API_ORIGIN = "https://api.atlassian.com"
JIRA_AUTHORIZE = f"{ATLASSIAN_AUTH_ORIGIN}/authorize"
JIRA_TOKEN = f"{ATLASSIAN_AUTH_ORIGIN}/oauth/token"
JIRA_RESOURCES = f"{ATLASSIAN_API_ORIGIN}/oauth/token/accessible-resources"
_MAX_ROWS = 30
_MAX_COLS = 10


class JiraAdapter(ProviderAdapter):
    provider_id = "jira"
    auth_kind = "oauth"
    allowed_origins = (ATLASSIAN_AUTH_ORIGIN, ATLASSIAN_API_ORIGIN)

    def _client_id(self, config: Dict[str, Any]) -> str:
        cid = (config.get("client_id") or "").strip() or (os.getenv("JIRA_CLIENT_ID") or "").strip()
        if not cid:
            raise ValueError("Jira connector requires a client_id")
        return cid

    def _cloud_id(self, config: Dict[str, Any]) -> str:
        cid = (config.get("allowed_cloud_id") or config.get("cloud_id") or "").strip()
        if not cid:
            raise ValueError("Jira connector requires a configured cloud id")
        return cid

    # ── OAuth ────────────────────────────────────────────────────────────────
    def authorize_url(self, *, config, manifest, redirect_uri, state, code_challenge, nonce) -> str:
        return oauth.build_authorize_url(
            authorize_endpoint=JIRA_AUTHORIZE,
            client_id=self._client_id(config),
            redirect_uri=redirect_uri,
            scopes=manifest.get("scopes", []),
            state=state,
            code_challenge=code_challenge,
            extra={"audience": "api.atlassian.com", "prompt": "consent"},
        )

    async def exchange_code(self, *, config, manifest, client_secret, code, redirect_uri, code_verifier) -> TokenResult:
        body = await egress.request_json(
            "POST", JIRA_TOKEN, allowed_origins=self.allowed_origins,
            json={
                "grant_type": "authorization_code",
                "client_id": self._client_id(config),
                "client_secret": client_secret or "",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        return await self._to_result(body)

    async def refresh(self, *, config, manifest, client_secret, refresh_token) -> TokenResult:
        body = await egress.request_json(
            "POST", JIRA_TOKEN, allowed_origins=self.allowed_origins,
            json={
                "grant_type": "refresh_token",
                "client_id": self._client_id(config),
                "client_secret": client_secret or "",
                "refresh_token": refresh_token,
            },
        )
        return await self._to_result(body)

    async def _to_result(self, body: Dict[str, Any]) -> TokenResult:
        access = str(body.get("access_token") or "")
        if not access:
            raise oauth.OAuthError(f"Jira OAuth error: {body.get('error') or 'no access_token'}")
        # Resolve accessible sites so we can bind + validate the cloud id.
        sites: List[Dict[str, Any]] = []
        try:
            res = await egress.request(
                "GET", JIRA_RESOURCES, allowed_origins=self.allowed_origins,
                headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
            )
            data = res.json() if res.content else []
            if isinstance(data, list):
                sites = data
        except Exception as exc:  # noqa: BLE001
            logger.debug("jira accessible-resources failed: %s", exc)
        return TokenResult(
            access_token=access,
            refresh_token=body.get("refresh_token"),
            expires_in=body.get("expires_in"),
            scope=body.get("scope"),
            id_token=None,
            claims={
                "cloud_ids": [str(s.get("id") or "") for s in sites if s.get("id")],
                "sites": [{"id": s.get("id"), "url": s.get("url"), "name": s.get("name")} for s in sites],
            },
        )

    def bound_account(self, token: TokenResult) -> Dict[str, str]:
        sites = (token.claims or {}).get("sites") or []
        site = sites[0] if sites else {}
        return {
            "tenant_id": str(site.get("id") or ""),
            "object_id": str(site.get("id") or ""),
            "upn": str(site.get("url") or site.get("name") or "jira"),
        }

    def validate_and_bind(self, token, *, config, expected_nonce, expected_tenant, expected_object_id) -> Dict[str, str]:
        # External identity: bind the Atlassian SITE, not Entra ids. Require the
        # granted token to actually reach the admin-configured cloud id.
        if not token.access_token:
            raise ValueError("Jira did not return an access token")
        cloud_ids = set((token.claims or {}).get("cloud_ids") or [])
        want = (config.get("allowed_cloud_id") or config.get("cloud_id") or "").strip()
        if want:
            if want not in cloud_ids:
                raise ValueError("Your Jira account cannot access the configured site")
            chosen = want
        else:
            if not cloud_ids:
                raise ValueError("No accessible Jira site for this account")
            chosen = next(iter(cloud_ids))
        sites = {str(s.get("id")): s for s in (token.claims or {}).get("sites") or []}
        site = sites.get(chosen, {})
        return {
            "tenant_id": chosen,
            "object_id": chosen,
            "upn": str(site.get("url") or site.get("name") or chosen),
        }

    # ── Action execution ─────────────────────────────────────────────────────
    async def execute(self, *, action, params, snapshot_payload, config, access_token=None, api_key=None) -> Dict[str, Any]:
        if action != "create_issue":
            raise ValueError(f"Unsupported action for jira: {action}")
        if not access_token:
            raise ValueError("Jira requires an access token")
        snapshot_payload = snapshot_payload or {}
        cloud_id = self._cloud_id(config)
        url = f"{ATLASSIAN_API_ORIGIN}/ex/jira/{cloud_id}/rest/api/3/issue"
        description = self._render_adf(
            note=params.get("note") or "",
            columns=snapshot_payload.get("columns", []),
            rows=snapshot_payload.get("rows", []),
        )
        payload = {
            "fields": {
                "project": {"key": params["project_key"]},
                "issuetype": {"name": params["issue_type"]},
                "summary": params["summary"],
                "description": description,
            }
        }
        body = await egress.request_json(
            "POST", url, allowed_origins=self.allowed_origins,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
        )
        status = int(body.get("_status_code", 0) or 0)
        accepted = status in (200, 201) and bool(body.get("key"))
        return {
            "status_code": status,
            "accepted": accepted,
            "provider": "jira",
            "issue_key": body.get("key"),
            "error": None if accepted else str(body.get("errorMessages") or body.get("errors") or "create_failed"),
        }

    def _render_adf(self, *, note: str, columns: List[Any], rows: List[Any]) -> Dict[str, Any]:
        """Render an Atlassian Document Format description (server-owned, escaped)."""
        content: List[Dict[str, Any]] = []
        if note:
            content.append({"type": "paragraph", "content": [{"type": "text", "text": note[:1000]}]})
        col_names = [self._col_name(c) for c in columns[:_MAX_COLS]]
        lines = [" | ".join(col_names)]
        for row in rows[:_MAX_ROWS]:
            cells = row[:_MAX_COLS] if isinstance(row, (list, tuple)) else [row]
            lines.append(" | ".join("" if v is None else str(v) for v in cells))
        if len(rows) > _MAX_ROWS:
            lines.append(f"… {len(rows) - _MAX_ROWS} more row(s)")
        content.append({
            "type": "codeBlock",
            "content": [{"type": "text", "text": "\n".join(lines)[:8000]}],
        })
        content.append({"type": "paragraph", "content": [{"type": "text", "text": "Created from Jeen Insights."}]})
        return {"type": "doc", "version": 1, "content": content}

    @staticmethod
    def _col_name(c: Any) -> str:
        if isinstance(c, dict):
            return str(c.get("name") or c.get("column") or "")
        return str(c)
