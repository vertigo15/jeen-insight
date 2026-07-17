"""Recipient normalization + policy validation for outbound connector actions.

Recipients are collected and validated on the server (never taken from the LLM).
Validation enforces syntactic sanity, IDN/domain normalization, a per-connector
recipient-domain allowlist, and flags external recipients.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

# Deliberately conservative: one @, a dotted domain, no spaces/control chars.
_EMAIL_RE = re.compile(r"^[^\s@]+@([^\s@]+\.[^\s@]+)$")

MAX_RECIPIENTS = 25


@dataclass
class RecipientValidation:
    valid: List[str] = field(default_factory=list)
    invalid: List[str] = field(default_factory=list)
    external: List[str] = field(default_factory=list)  # allowed but outside sender domain
    rejected: List[str] = field(default_factory=list)   # blocked by allowlist policy

    @property
    def ok(self) -> bool:
        return not self.invalid and not self.rejected and bool(self.valid)


def _normalize(addr: str) -> Optional[str]:
    addr = (addr or "").strip().lower()
    if not addr:
        return None
    m = _EMAIL_RE.match(addr)
    if not m:
        return None
    domain = m.group(1)
    try:
        domain = domain.encode("idna").decode("ascii")
    except Exception:  # noqa: BLE001
        return None
    local = addr.rsplit("@", 1)[0]
    return f"{local}@{domain}"


def domain_of(addr: str) -> str:
    return addr.rsplit("@", 1)[1] if "@" in addr else ""


def validate_recipients(
    raw: List[str],
    *,
    sender_domain: str,
    allowlist: Optional[List[str]] = None,
    allow_external: bool = False,
) -> RecipientValidation:
    """Validate + classify recipients against connector policy.

    - allowlist empty and allow_external False => only the sender's own domain.
    - allowlist non-empty => those domains (plus the sender domain) are allowed.
    - allow_external True => any syntactically valid domain is allowed but flagged.
    """
    result = RecipientValidation()
    sender_domain = (sender_domain or "").strip().lower()
    allowed_domains = {d.strip().lower() for d in (allowlist or []) if d.strip()}
    if sender_domain:
        allowed_domains.add(sender_domain)

    seen = set()
    for item in raw[:MAX_RECIPIENTS]:
        norm = _normalize(item)
        if not norm:
            result.invalid.append(item)
            continue
        if norm in seen:
            continue
        seen.add(norm)
        dom = domain_of(norm)
        is_external = dom != sender_domain
        if allow_external:
            result.valid.append(norm)
            if is_external:
                result.external.append(norm)
        elif dom in allowed_domains:
            result.valid.append(norm)
            if is_external:
                result.external.append(norm)
        else:
            result.rejected.append(norm)

    # Over-limit inputs are treated as invalid so callers see them.
    for extra in raw[MAX_RECIPIENTS:]:
        result.invalid.append(extra)
    return result
