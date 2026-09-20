"""Generic OpenID Connect sign-in for the Flask UI (Keycloak, Zitadel, ...).

One module serves every OIDC provider; Microsoft Entra keeps its own MSAL path
in :mod:`src.entra_auth`. A provider ``<p>`` (lower-case key, default set
``keycloak,zitadel``, override with ``OIDC_PROVIDERS``) is enabled when both
``OIDC_<P>_ISSUER`` and ``OIDC_<P>_CLIENT_ID`` are set. Optional per provider:

- ``OIDC_<P>_CLIENT_SECRET`` — confidential client (``client_secret_basic``);
  without it the client is public. PKCE (S256) is always used.
- ``OIDC_<P>_LABEL`` — button text; defaults to the capitalised key.
- ``OIDC_<P>_SCOPES`` — space separated; default ``openid profile email``
  (Zitadel additionally gets its project-roles scope).
- ``OIDC_<P>_IDP_HINT`` — Keycloak ``kc_idp_hint`` (e.g. ``entra-id``) so the
  button goes straight to a brokered identity provider.
- ``OIDC_<P>_ADMIN_ROLES`` / ``OIDC_<P>_EDITOR_ROLES`` — comma lists. When either
  is set the provider is authoritative for the application role on every
  sign-in (admin > editor > viewer); when neither is set the account keeps
  its database role (new accounts are viewers).

TLS: internal identity providers often run on a private CA. ``OIDC_CA_BUNDLE``
names a PEM file trusted *in addition to* the system store; ``OIDC_TLS_VERIFY=false``
disables verification altogether (development only, logged loudly).

The flow is the standard authorization-code flow with ``state``, ``nonce`` and
PKCE; the ID token is validated against the provider's JWKS (signature, issuer,
audience, expiry, nonce). Nothing here touches the database — the routes in
``src/ui_app.py`` provision the account.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

import httpx
import jwt

logger = logging.getLogger(__name__)

DEFAULT_PROVIDERS = ("keycloak", "zitadel")
DEFAULT_SCOPES = ("openid", "profile", "email")
ZITADEL_ROLES_SCOPE = "urn:zitadel:iam:org:project:roles"
ALLOWED_ALGS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")
DISCOVERY_TTL_SECONDS = 3600
HTTP_TIMEOUT_SECONDS = 10.0
ROLE_ORDER = ("admin", "editor", "viewer")


class OidcError(RuntimeError):
    """A sign-in step failed; the message is safe to show to the user."""


@dataclass(frozen=True)
class OidcProvider:
    key: str
    label: str
    issuer: str
    client_id: str
    client_secret: str = ""
    scopes: Tuple[str, ...] = DEFAULT_SCOPES
    idp_hint: str = ""
    admin_roles: frozenset = field(default_factory=frozenset)
    editor_roles: frozenset = field(default_factory=frozenset)

    @property
    def confidential(self) -> bool:
        return bool(self.client_secret)

    @property
    def maps_roles(self) -> bool:
        return bool(self.admin_roles or self.editor_roles)

    def redirect_uri(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/auth/{self.key}/callback"


# ── Configuration ────────────────────────────────────────────────────────────

def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _csv(value: str) -> frozenset:
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def _valid_key(key: str) -> bool:
    return bool(key) and all(ch.isalnum() or ch in "-_" for ch in key) and key.islower()


def provider_from_env(key: str) -> Optional[OidcProvider]:
    """Build one provider from ``OIDC_<KEY>_*``; None when not (fully) configured."""
    if not _valid_key(key):
        return None
    prefix = f"OIDC_{key.upper().replace('-', '_')}_"
    issuer = _env(prefix + "ISSUER").rstrip("/")
    client_id = _env(prefix + "CLIENT_ID")
    if not issuer or not client_id:
        return None
    if not issuer.startswith("https://") and not issuer.startswith("http://localhost"):
        logger.error("oidc[%s]: issuer must be https (%s); provider disabled", key, issuer)
        return None
    scopes: Tuple[str, ...] = tuple(_env(prefix + "SCOPES").split()) or DEFAULT_SCOPES
    if key == "zitadel" and ZITADEL_ROLES_SCOPE not in scopes:
        scopes = scopes + (ZITADEL_ROLES_SCOPE,)
    return OidcProvider(
        key=key,
        label=_env(prefix + "LABEL") or key.replace("-", " ").replace("_", " ").title(),
        issuer=issuer,
        client_id=client_id,
        client_secret=_env(prefix + "CLIENT_SECRET"),
        scopes=scopes,
        idp_hint=_env(prefix + "IDP_HINT"),
        admin_roles=_csv(_env(prefix + "ADMIN_ROLES")),
        editor_roles=_csv(_env(prefix + "EDITOR_ROLES")),
    )


def configured_providers() -> List[OidcProvider]:
    """Enabled providers in display order (``OIDC_PROVIDERS`` or the defaults)."""
    keys = [k.strip().lower() for k in (_env("OIDC_PROVIDERS") or ",".join(DEFAULT_PROVIDERS)).split(",")]
    providers: List[OidcProvider] = []
    for key in keys:
        provider = provider_from_env(key)
        if provider:
            providers.append(provider)
    return providers


def get_provider(key: str) -> Optional[OidcProvider]:
    for provider in configured_providers():
        if provider.key == key:
            return provider
    return None


def is_configured() -> bool:
    return bool(configured_providers())


# ── HTTP / TLS ───────────────────────────────────────────────────────────────

_ssl_lock = threading.Lock()
_ssl_cache: Dict[str, ssl.SSLContext] = {}


def _tls_verify() -> bool:
    return _env("OIDC_TLS_VERIFY").lower() not in ("0", "false", "no", "off")


def _ssl_context() -> ssl.SSLContext | bool:
    """System trust store plus ``OIDC_CA_BUNDLE``; ``False`` only when verification is disabled."""
    if not _tls_verify():
        logger.warning("oidc: OIDC_TLS_VERIFY=false — identity provider TLS is NOT verified (development only)")
        return False
    bundle = _env("OIDC_CA_BUNDLE")
    cache_key = bundle or "<system>"
    with _ssl_lock:
        ctx = _ssl_cache.get(cache_key)
        if ctx is None:
            ctx = ssl.create_default_context()
            if bundle:
                ctx.load_verify_locations(cafile=bundle)
            _ssl_cache[cache_key] = ctx
    return ctx


def _client() -> httpx.Client:
    return httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, verify=_ssl_context(), follow_redirects=False)


def _get_json(url: str) -> Dict[str, Any]:
    with _client() as client:
        response = client.get(url, headers={"Accept": "application/json"})
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise OidcError(f"unexpected response from {url}")
    return data


# ── Discovery and keys (cached per issuer) ───────────────────────────────────

_meta_lock = threading.Lock()
_meta_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_jwks_cache: Dict[str, Tuple[float, jwt.PyJWKSet]] = {}


def discovery(provider: OidcProvider, *, force: bool = False) -> Dict[str, Any]:
    """The provider's OpenID configuration; refreshed hourly."""
    now = time.time()
    with _meta_lock:
        cached = _meta_cache.get(provider.issuer)
        if cached and not force and now - cached[0] < DISCOVERY_TTL_SECONDS:
            return cached[1]
    try:
        meta = _get_json(f"{provider.issuer}/.well-known/openid-configuration")
    except (httpx.HTTPError, OidcError) as exc:
        raise OidcError(f"{provider.label} is not reachable ({exc.__class__.__name__})") from exc
    issuer = str(meta.get("issuer") or "").rstrip("/")
    if issuer != provider.issuer:
        raise OidcError(f"{provider.label} discovery issuer mismatch: {issuer!r} != {provider.issuer!r}")
    for field_name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not meta.get(field_name):
            raise OidcError(f"{provider.label} discovery lacks {field_name}")
    with _meta_lock:
        _meta_cache[provider.issuer] = (now, meta)
    return meta


def jwks(provider: OidcProvider, *, force: bool = False) -> jwt.PyJWKSet:
    now = time.time()
    with _meta_lock:
        cached = _jwks_cache.get(provider.issuer)
        if cached and not force and now - cached[0] < DISCOVERY_TTL_SECONDS:
            return cached[1]
    uri = discovery(provider)["jwks_uri"]
    try:
        keyset = jwt.PyJWKSet.from_dict(_get_json(uri))
    except (httpx.HTTPError, OidcError, jwt.PyJWTError) as exc:
        raise OidcError(f"{provider.label} signing keys unavailable ({exc.__class__.__name__})") from exc
    with _meta_lock:
        _jwks_cache[provider.issuer] = (now, keyset)
    return keyset


def reset_caches() -> None:
    """Forget discovery/JWKS/TLS caches (tests, or after a provider change)."""
    with _meta_lock:
        _meta_cache.clear()
        _jwks_cache.clear()
    with _ssl_lock:
        _ssl_cache.clear()


# ── Authorization request ────────────────────────────────────────────────────

def new_state() -> str:
    return secrets.token_urlsafe(32)


def new_nonce() -> str:
    return secrets.token_urlsafe(32)


def new_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorization_url(
    provider: OidcProvider,
    *,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_verifier: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(provider.scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge(code_verifier),
        "code_challenge_method": "S256",
    }
    if provider.idp_hint:
        params["kc_idp_hint"] = provider.idp_hint
    endpoint = discovery(provider)["authorization_endpoint"]
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}{urlencode(params)}"


# ── Token exchange and validation ────────────────────────────────────────────

def exchange_code(
    provider: OidcProvider,
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> Dict[str, Any]:
    """Redeem the authorization code; returns the token response (must carry ``id_token``)."""
    endpoint = discovery(provider)["token_endpoint"]
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    auth = None
    if provider.confidential:
        auth = httpx.BasicAuth(provider.client_id, provider.client_secret)
    else:
        data["client_id"] = provider.client_id
    try:
        with _client() as client:
            response = client.post(endpoint, data=data, auth=auth, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise OidcError(f"{provider.label} token endpoint unreachable ({exc.__class__.__name__})") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code != 200 or not isinstance(payload, dict):
        reason = str(payload.get("error_description") or payload.get("error") or f"HTTP {response.status_code}") if isinstance(payload, dict) else f"HTTP {response.status_code}"
        raise OidcError(f"{provider.label} rejected the authorization code: {reason}")
    if not payload.get("id_token"):
        raise OidcError(f"{provider.label} returned no ID token")
    return payload


def validate_id_token(provider: OidcProvider, id_token: str, *, nonce: str) -> Dict[str, Any]:
    """Verify signature, issuer, audience, expiry and nonce; return the claims."""
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise OidcError(f"{provider.label} returned a malformed ID token") from exc
    alg = str(header.get("alg") or "")
    if alg not in ALLOWED_ALGS:
        raise OidcError(f"{provider.label} ID token uses unsupported algorithm {alg!r}")
    kid = header.get("kid")

    def _key(force: bool):
        keyset = jwks(provider, force=force)
        try:
            return keyset[kid] if kid else keyset.keys[0]
        except (KeyError, IndexError):
            return None

    signing_key = _key(False) or _key(True)
    if signing_key is None:
        raise OidcError(f"{provider.label} ID token signed with an unknown key")

    expected_issuer = str(discovery(provider).get("issuer") or provider.issuer)
    try:
        claims = jwt.decode(
            id_token,
            key=signing_key.key,
            algorithms=[alg],
            audience=provider.client_id,
            issuer=expected_issuer,
            leeway=60,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise OidcError(f"{provider.label} ID token failed validation ({exc.__class__.__name__})") from exc

    if not nonce or claims.get("nonce") != nonce:
        raise OidcError(f"{provider.label} ID token nonce mismatch")
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") not in (None, provider.client_id):
        raise OidcError(f"{provider.label} ID token authorized party mismatch")
    return claims


# ── Claims → application identity ────────────────────────────────────────────

def profile_from_claims(claims: Dict[str, Any]) -> Dict[str, str]:
    """``{"email", "name", "sub"}`` from standard claims (email may be empty)."""
    email = ""
    for candidate in (claims.get("email"), claims.get("preferred_username"), claims.get("upn")):
        if isinstance(candidate, str) and "@" in candidate:
            email = candidate.strip().lower()
            break
    name = claims.get("name")
    if not name:
        parts = [claims.get("given_name"), claims.get("family_name")]
        name = " ".join(p for p in parts if isinstance(p, str) and p.strip())
    if not name:
        name = claims.get("preferred_username") or email
    return {"email": email, "name": str(name or "").strip(), "sub": str(claims.get("sub") or "")}


def roles_from_claims(provider: OidcProvider, claims: Dict[str, Any]) -> frozenset:
    """Role/group names from the places Keycloak and Zitadel put them."""
    found: set = set()

    def _add(values: Any) -> None:
        if isinstance(values, str):
            found.add(values.lstrip("/"))
        elif isinstance(values, dict):
            found.update(str(k) for k in values.keys())
        elif isinstance(values, Iterable):
            for v in values:
                if isinstance(v, str):
                    found.add(v.lstrip("/"))

    realm = claims.get("realm_access")
    if isinstance(realm, dict):
        _add(realm.get("roles"))
    resource = claims.get("resource_access")
    if isinstance(resource, dict):
        client = resource.get(provider.client_id)
        if isinstance(client, dict):
            _add(client.get("roles"))
    _add(claims.get("roles"))
    _add(claims.get("groups"))
    for key, value in claims.items():
        if isinstance(key, str) and key.startswith("urn:zitadel:iam:org:project") and key.endswith(":roles"):
            _add(value)
    return frozenset(found)


def mapped_role(provider: OidcProvider, claims: Dict[str, Any]) -> Optional[str]:
    """Application role dictated by the provider, or None when it does not map roles."""
    if not provider.maps_roles:
        return None
    roles = roles_from_claims(provider, claims)
    if roles & provider.admin_roles:
        return "admin"
    if roles & provider.editor_roles:
        return "editor"
    return "viewer"
