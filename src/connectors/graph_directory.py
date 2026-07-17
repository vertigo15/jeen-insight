"""App-only (client-credentials) Microsoft Graph directory reads.

Used to *authoritatively* revalidate a user's Entra group membership between
interactive logins, so group removals revoke connector access within a bounded
window rather than waiting for the user's session/token to expire.

Best-effort by design: if app credentials or the Graph directory permission
(``GroupMember.Read.All`` / ``Directory.Read.All``, admin-consented) are not
available, callers fall back to login-time token claims (which still age out via
the membership TTL). This module never trusts partial/failed reads — a failure
raises so the caller keeps the previous (login-bounded) state instead of marking
membership authoritatively fresh.
"""

from __future__ import annotations

import logging
import os
import time
from typing import List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

_GRAPH = "https://graph.microsoft.com/v1.0"


class GraphDirectoryError(RuntimeError):
    pass


class GraphDirectoryClient:
    """Fetches transitive group membership using an app-only Graph token."""

    def __init__(
        self,
        *,
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ) -> None:
        self._tenant = (tenant_id or os.getenv("AZURE_AD_TENANT_ID") or os.getenv("CONNECTORS_TENANT_ID") or "").strip()
        self._client_id = (client_id or os.getenv("AZURE_AD_CLIENT_ID") or "").strip()
        self._client_secret = (client_secret or os.getenv("AZURE_AD_CLIENT_SECRET") or "").strip()
        self._token: Optional[str] = None
        self._token_exp: float = 0.0

    def available(self) -> bool:
        return bool(self._tenant and self._client_id and self._client_secret)

    async def _access_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_exp - 60:
            return self._token
        if not self.available():
            raise GraphDirectoryError("App-only Graph credentials are not configured")
        token_endpoint = f"https://login.microsoftonline.com/{self._tenant}/oauth2/v2.0/token"
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(token_endpoint, data=data)
        if resp.status_code != 200:
            raise GraphDirectoryError(f"client_credentials token failed ({resp.status_code})")
        body = resp.json()
        self._token = body.get("access_token") or ""
        self._token_exp = now + float(body.get("expires_in") or 0)
        if not self._token:
            raise GraphDirectoryError("client_credentials returned no access_token")
        return self._token

    async def member_group_ids(self, object_id: str) -> Tuple[List[str], bool]:
        """Return (group_object_ids, complete) for a user via transitiveMemberOf.

        ``complete`` is True only when the full result set was read without error.
        Raises :class:`GraphDirectoryError` on any failure so the caller does not
        treat an incomplete read as authoritative.
        """
        if not object_id:
            raise GraphDirectoryError("object_id required")
        token = await self._access_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            f"{_GRAPH}/users/{object_id}/transitiveMemberOf/microsoft.graph.group"
            "?$select=id&$top=999"
        )
        group_ids: List[str] = []
        async with httpx.AsyncClient(timeout=20) as client:
            while url:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    raise GraphDirectoryError(f"memberOf read failed ({resp.status_code})")
                body = resp.json()
                for item in body.get("value", []):
                    gid = str(item.get("id") or "").strip()
                    if gid:
                        group_ids.append(gid)
                url = body.get("@odata.nextLink")
        return group_ids, True
