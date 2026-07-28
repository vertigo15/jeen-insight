"""Server-owned outbound HTTP for connector providers (SSRF/egress control).

Every provider that talks to an external API MUST go through :func:`request` (or
:func:`request_json`). This centralizes the outbound-security controls the plan
requires so no provider can be tricked (by config, an LLM argument, or a provider
response) into calling an attacker-controlled or internal host:

  * Fixed, per-call HTTPS origin allowlist — the caller passes the exact origins
    it is permitted to reach; anything else raises before a socket opens.
  * HTTPS only. No plaintext HTTP.
  * No redirects (``follow_redirects=False``) — a 3xx to an internal host is a
    classic SSRF pivot.
  * ``trust_env=False`` — ignore ambient proxies / ``*_PROXY`` env so traffic
    can't be silently rerouted.
  * Hard timeouts and a response-size cap (stream + abort) to bound cost/DoS.
  * Best-effort private/loopback/link-local IP rejection for the target host.

The origin allowlist is the primary control; the IP check is defense in depth
(hostnames here are hardcoded by providers, never user-supplied URLs).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any, Dict, Mapping, Optional, Sequence
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20.0
# Cap any single response we will buffer (256 KiB is ample for JSON control-plane
# calls; read tools that fetch larger bodies pass their own, still-bounded cap).
DEFAULT_MAX_BYTES = 256 * 1024


class EgressError(RuntimeError):
    """Raised when an outbound request violates the egress policy."""


class ResponseTooLarge(EgressError):
    pass


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _assert_allowed(url: str, allowed_origins: Sequence[str]) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise EgressError(f"Refusing non-HTTPS egress to {url!r}")
    if not parts.hostname:
        raise EgressError(f"Egress URL has no host: {url!r}")
    origin = _origin(url)
    allow = {o.lower().rstrip("/") for o in allowed_origins}
    if origin.rstrip("/") not in allow:
        raise EgressError(f"Egress origin {origin!r} is not in the allowlist")
    _assert_not_private(parts.hostname)


def _assert_not_private(host: str) -> None:
    """Reject hosts that resolve to a private/loopback/link-local/reserved IP."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except Exception:  # noqa: BLE001 - DNS failure surfaces later as a connect error
        return
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise EgressError(f"Egress host {host!r} resolves to a blocked address ({addr})")


async def request(
    method: str,
    url: str,
    *,
    allowed_origins: Sequence[str],
    headers: Optional[Mapping[str, str]] = None,
    json: Any = None,
    data: Any = None,
    params: Optional[Mapping[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> httpx.Response:
    """Perform a hardened outbound request. Raises :class:`EgressError` on policy
    violation and :class:`ResponseTooLarge` when the body exceeds ``max_bytes``."""
    _assert_allowed(url, allowed_origins)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, trust_env=False
    ) as client:
        async with client.stream(
            method, url, headers=dict(headers or {}), json=json, data=data, params=params
        ) as resp:
            chunks = bytearray()
            async for chunk in resp.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > max_bytes:
                    raise ResponseTooLarge(
                        f"Response from {_origin(url)} exceeded {max_bytes} bytes"
                    )
            # Rebuild a Response with the buffered content so callers can read
            # .json()/.text/.status_code after the stream context closes.
            #
            # ``aiter_bytes()`` already DECODED the body per Content-Encoding, so
            # the buffered bytes are identity. We must drop the content-coding
            # headers before handing them to the constructor: httpx.Response(content=)
            # eagerly calls .read(), which would otherwise re-apply the gzip/deflate
            # decoder to already-decompressed bytes and raise DecodingError
            # ("incorrect header check"). Content-Length is likewise stale.
            _stale = {"content-encoding", "content-length"}
            buffered_headers = [
                (name, value)
                for name, value in resp.headers.multi_items()
                if name.lower() not in _stale
            ]
            return httpx.Response(
                status_code=resp.status_code,
                headers=buffered_headers,
                content=bytes(chunks),
                request=resp.request,
            )


async def request_json(
    method: str,
    url: str,
    *,
    allowed_origins: Sequence[str],
    **kwargs: Any,
) -> Dict[str, Any]:
    """Like :func:`request` but returns parsed JSON (``{}`` when the body is empty)."""
    resp = await request(method, url, allowed_origins=allowed_origins, **kwargs)
    if not resp.content:
        return {"_status_code": resp.status_code}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"_raw": resp.text[:2000]}
    if isinstance(body, dict):
        body.setdefault("_status_code", resp.status_code)
        return body
    return {"_status_code": resp.status_code, "_data": body}
