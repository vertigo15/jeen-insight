"""Internal service-to-service auth: Flask mints, FastAPI verifies.

Flask is the SOLE token issuer. On every upstream call it mints a short-lived,
audience-bound, HMAC-signed token carrying the authenticated session identity.
FastAPI verifies the token into a server-side :class:`Principal` and derives all
identity/role/group facts from it — never from request bodies or query params.

Rotation: ``INTERNAL_API_SECRET`` may list ``<kid>:<secret>`` pairs
(comma-separated). The first entry signs; all entries verify. When unset it
falls back to ``FLASK_SECRET_KEY`` / ``AUTH_SECRET`` so a correctly-configured
deployment shares the secret across both containers automatically.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

AUDIENCE_API = "jeen-insights-api"
_SALT = "jeen-insights-internal-v1"
DEFAULT_TTL_SECONDS = 120
# Same insecure dev key Flask falls back to; keeps local HTTP dev working.
_DEV_SECRET = "jeen-insights-dev-only-insecure-secret"  # noqa: S105
# Known placeholders that must never sign tokens in production.
_INSECURE_SECRETS = {
    _DEV_SECRET,
    "jeen-insights-change-me-in-production",
    "change-me-generate-a-random-48+-char-value",
}


class InternalAuthConfigError(RuntimeError):
    """Raised at startup when internal-auth secrets are unsafe for production."""


def _dev_mode() -> bool:
    raw = (os.getenv("JEEN_DEV_MODE") or "").strip().lower()
    return raw in ("1", "true", "yes", "on", "t")


class PrincipalError(Exception):
    """Raised when an internal token is missing, invalid, or expired."""


@dataclass(frozen=True)
class Principal:
    """Server-side authenticated identity, derived only from a verified token."""

    user_id: str
    role: str = "viewer"
    name: str = ""
    email: str = ""
    tenant_id: str = ""
    object_id: str = ""  # Entra object id when signed in via SSO
    groups: Tuple[str, ...] = field(default_factory=tuple)  # Entra group object ids
    groups_complete: bool = True  # False on Graph overage/truncation -> fail closed
    auth_provider: str = "local"
    auth_time: int = 0  # epoch secs of the interactive login; bounds group-claim trust

    @property
    def is_entra(self) -> bool:
        return bool(self.tenant_id and self.object_id)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_editor(self) -> bool:
        return self.role in ("admin", "editor")

    def to_user_context(self) -> Dict[str, str]:
        return {
            "user_id": self.user_id,
            "user_name": self.name,
            "user_email": self.email,
        }


def _load_secrets() -> List[Tuple[str, str]]:
    """Return ordered (kid, secret). First entry signs; all verify.

    Fails closed in production: if neither INTERNAL_API_SECRET nor a strong
    FLASK_SECRET_KEY/AUTH_SECRET is configured, raise rather than silently sign
    with a predictable key (which would let an attacker forge a Principal).
    """
    raw = (os.getenv("INTERNAL_API_SECRET") or "").strip()
    if not raw:
        fallback = (os.getenv("FLASK_SECRET_KEY") or os.getenv("AUTH_SECRET") or "").strip()
        if fallback and fallback not in _INSECURE_SECRETS:
            return [("primary", fallback)]
        if _dev_mode():
            # Dev fallback — matches ui_app's insecure dev key so local dev works.
            return [("dev", _DEV_SECRET)]
        raise InternalAuthConfigError(
            "INTERNAL_API_SECRET (or a strong FLASK_SECRET_KEY/AUTH_SECRET) is "
            "required. Refusing to sign/verify internal tokens with a missing or "
            "known placeholder key. Set JEEN_DEV_MODE=true for local dev only."
        )
    out: List[Tuple[str, str]] = []
    for i, part in enumerate(p for p in raw.split(",") if p.strip()):
        part = part.strip()
        if ":" in part:
            kid, secret = part.split(":", 1)
            out.append((kid.strip() or f"k{i}", secret.strip()))
        else:
            out.append(("primary" if i == 0 else f"k{i}", part))
    if not out:
        raise InternalAuthConfigError("INTERNAL_API_SECRET is set but empty after parsing.")
    return out


def assert_configured() -> None:
    """Validate secret configuration at startup (raises InternalAuthConfigError)."""
    _load_secrets()


def is_enforced() -> bool:
    """True when FastAPI should default-deny requests lacking a valid token."""
    raw = (os.getenv("INTERNAL_AUTH_ENABLED") or "true").strip().lower()
    return raw in ("1", "true", "yes", "on", "t")


def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=_SALT)


def issue_internal_token(
    principal_claims: Dict[str, Any],
    *,
    audience: str = AUDIENCE_API,
) -> str:
    """Mint a signed token embedding ``principal_claims`` and the audience.

    ``principal_claims`` should contain at least ``user_id`` plus any of
    ``role``/``name``/``email``/``tenant_id``/``object_id``/``groups``/
    ``auth_provider``.
    """
    kid, secret = _load_secrets()[0]
    payload = {
        "kid": kid,
        "aud": audience,
        "sub": str(principal_claims.get("user_id") or ""),
        "role": principal_claims.get("role") or "viewer",
        "name": principal_claims.get("name") or "",
        "email": principal_claims.get("email") or "",
        "tid": principal_claims.get("tenant_id") or "",
        "oid": principal_claims.get("object_id") or "",
        "groups": list(principal_claims.get("groups") or []),
        "gc": bool(principal_claims.get("groups_complete", True)),
        "prov": principal_claims.get("auth_provider") or "local",
        "lat": int(principal_claims.get("auth_time") or 0),
    }
    return _serializer(secret).dumps(payload)


def verify_internal_token(
    token: str,
    *,
    audience: str = AUDIENCE_API,
    max_age: int = DEFAULT_TTL_SECONDS,
) -> Principal:
    """Verify a token and return the :class:`Principal`, or raise PrincipalError."""
    if not token:
        raise PrincipalError("Missing internal token")

    last_err: Optional[Exception] = None
    for _kid, secret in _load_secrets():
        try:
            data = _serializer(secret).loads(token, max_age=max_age)
        except SignatureExpired as exc:
            raise PrincipalError("Internal token expired") from exc
        except BadSignature as exc:
            last_err = exc
            continue
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue

        if not isinstance(data, dict):
            raise PrincipalError("Malformed internal token payload")
        if data.get("aud") != audience:
            raise PrincipalError("Internal token audience mismatch")
        sub = str(data.get("sub") or "").strip()
        if not sub or sub == "default":
            raise PrincipalError("Internal token has no subject")
        return Principal(
            user_id=sub,
            role=str(data.get("role") or "viewer"),
            name=str(data.get("name") or ""),
            email=str(data.get("email") or ""),
            tenant_id=str(data.get("tid") or ""),
            object_id=str(data.get("oid") or ""),
            groups=tuple(str(g) for g in (data.get("groups") or [])),
            groups_complete=bool(data.get("gc", True)),
            auth_provider=str(data.get("prov") or "local"),
            auth_time=int(data.get("lat") or 0),
        )

    raise PrincipalError(f"Invalid internal token ({last_err or 'bad signature'})")
