"""Server-owned typed action policy for the connector platform.

This module is the SINGLE source of truth for what each connector action needs
and how its parameters are validated. Every authorization-relevant property —
the auth model, whether a result snapshot is mandatory, whether a per-user OAuth
grant is required, the outbound data class, and the parameter validator — is
resolved here from the ``(connector_key, action)`` pair.

Security invariants (see the GPT-5.6 review):
  * These properties are NEVER read from an LLM argument, a request field, or a
    stored manifest value — so a poisoned data cell or a compromised manifest can
    never relax an OAuth or snapshot requirement.
  * Unknown ``(connector_key, action)`` pairs fail CLOSED (``get_action_policy``
    returns ``None`` and the gate rejects the proposal).
  * Validators treat all incoming params as hostile: strict types, hard caps,
    and normalization. They return ONLY the server-normalized params that may be
    executed (internal-only keys are prefixed with ``_`` and stripped before
    persistence by the gate).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from src.connectors.recipients import validate_recipients

# Version stamp folded into the approval hash so a policy change invalidates any
# in-flight (proposed-but-not-executed) approvals.
ACTION_POLICY_VERSION = "action-policy-v1"


class ActionPolicyError(ValueError):
    """Raised when action parameters fail server-side validation."""


Validator = Callable[[Dict[str, Any], Dict[str, Any], Optional[Dict[str, Any]]], Dict[str, Any]]


@dataclass(frozen=True)
class ActionPolicy:
    connector_key: str
    action: str
    auth_kind: str            # 'oauth' | 'api_key'
    requires_snapshot: bool   # the action renders a server-held result snapshot
    requires_grant: bool      # a per-user OAuth grant must be active
    egress_class: str         # 'result_data' | 'search_query' | 'none'
    validate: Validator       # (params, config, grant) -> normalized params
    # OAuth scopes this action needs; verified against the grant's scopes at
    # execute time (empty for api_key actions). Matched case-insensitively by the
    # trailing scope name (e.g. a grant "…/Mail.Send" satisfies "Mail.Send").
    required_scopes: tuple = ()
    # True for read/data tools whose result is fed back into a continuation with
    # tools DISABLED. Write/export tools are False.
    is_read: bool = False


# ── Validators ────────────────────────────────────────────────────────────────

def _validate_send_email(
    params: Dict[str, Any],
    config: Dict[str, Any],
    grant: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Validate + normalize send_email params against the server recipient policy.

    Recipients, subject and note are treated as untrusted input regardless of
    whether a human or the model supplied them. The sender domain and the
    domain/external policy come from the immutable connector config + the bound
    grant, never from the request.
    """
    subject = (params.get("subject") or "").strip()
    if not subject:
        raise ActionPolicyError("Subject is required")
    note = (params.get("note") or "").strip()

    raw_recipients = params.get("recipients") or []
    if isinstance(raw_recipients, str):
        raw_recipients = [r for r in raw_recipients.replace(";", ",").split(",")]
    if not raw_recipients:
        raise ActionPolicyError("At least one recipient is required")

    sender = ((grant or {}).get("external_account") or "").strip().lower()
    sender_domain = sender.split("@", 1)[1] if "@" in sender else ""
    vr = validate_recipients(
        [str(r) for r in raw_recipients],
        sender_domain=sender_domain,
        allowlist=config.get("recipient_domain_allowlist") or [],
        allow_external=bool(config.get("allow_external_recipients")),
    )
    if vr.invalid:
        raise ActionPolicyError(f"Invalid recipient(s): {', '.join(vr.invalid[:5])}")
    if vr.rejected:
        raise ActionPolicyError(
            f"Recipient domain not allowed by policy: {', '.join(vr.rejected[:5])}"
        )
    if not vr.valid:
        raise ActionPolicyError("No valid recipients")
    return {
        "recipients": vr.valid,
        "subject": subject[:200],
        "note": note[:2000],
        "_external": vr.external,
    }


def _str_field(params: Dict[str, Any], key: str, *, required: bool, max_len: int) -> str:
    v = params.get(key)
    if v is None:
        v = ""
    if not isinstance(v, (str, int, float)):
        raise ActionPolicyError(f"{key} must be text")
    s = str(v).strip()
    if required and not s:
        raise ActionPolicyError(f"{key} is required")
    return s[:max_len]


def _allowlist(config: Dict[str, Any], key: str) -> list:
    vals = config.get(key) or []
    if isinstance(vals, str):
        vals = [x for x in vals.replace(";", ",").split(",")]
    return [str(x).strip() for x in vals if str(x).strip()]


def _validate_slack_post(
    params: Dict[str, Any],
    config: Dict[str, Any],
    grant: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Validate Slack post_message: destination channel must pass the server-owned
    allowlist (fail closed when an allowlist is configured)."""
    channel = _str_field(params, "channel", required=True, max_len=200)
    note = _str_field(params, "note", required=False, max_len=3000)
    allowed = _allowlist(config, "allowed_channels")
    if allowed and channel.lower() not in {a.lower() for a in allowed}:
        raise ActionPolicyError("That Slack channel is not permitted by policy")
    return {"channel": channel, "note": note}


def _validate_tavily_search(
    params: Dict[str, Any],
    config: Dict[str, Any],
    grant: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Validate Tavily web_search: a bounded query + capped result count."""
    query = _str_field(params, "query", required=True, max_len=400)
    try:
        max_results = int(params.get("max_results") or 5)
    except (TypeError, ValueError):
        raise ActionPolicyError("max_results must be a number")
    max_results = max(1, min(max_results, 10))
    return {"query": query, "max_results": max_results}


def _validate_jira_issue(
    params: Dict[str, Any],
    config: Dict[str, Any],
    grant: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Validate Jira create_issue: project + issue type must pass the server-owned
    allowlists; the cloud id is fixed by config (never a user-supplied base URL)."""
    project = _str_field(params, "project_key", required=True, max_len=64).upper()
    issue_type = _str_field(params, "issue_type", required=True, max_len=64)
    summary = _str_field(params, "summary", required=True, max_len=255)
    note = _str_field(params, "note", required=False, max_len=3000)
    projects = _allowlist(config, "allowed_projects")
    if projects and project not in {p.upper() for p in projects}:
        raise ActionPolicyError("That Jira project is not permitted by policy")
    types = _allowlist(config, "allowed_issue_types")
    if types and issue_type.lower() not in {t.lower() for t in types}:
        raise ActionPolicyError("That Jira issue type is not permitted by policy")
    return {
        "project_key": project,
        "issue_type": issue_type,
        "summary": summary,
        "note": note,
    }


# ── Registry ──────────────────────────────────────────────────────────────────

_POLICIES: Dict[tuple, ActionPolicy] = {
    ("microsoft-graph-mail", "send_email"): ActionPolicy(
        connector_key="microsoft-graph-mail",
        action="send_email",
        auth_kind="oauth",
        requires_snapshot=True,
        requires_grant=True,
        egress_class="result_data",
        validate=_validate_send_email,
        required_scopes=("Mail.Send",),
    ),
    ("slack-message", "post_message"): ActionPolicy(
        connector_key="slack-message",
        action="post_message",
        auth_kind="oauth",
        requires_snapshot=True,
        requires_grant=True,
        egress_class="result_data",
        validate=_validate_slack_post,
        required_scopes=("chat:write",),
    ),
    ("jira-issue", "create_issue"): ActionPolicy(
        connector_key="jira-issue",
        action="create_issue",
        auth_kind="oauth",
        requires_snapshot=True,
        requires_grant=True,
        egress_class="result_data",
        validate=_validate_jira_issue,
        required_scopes=("write:jira-work",),
    ),
    ("tavily-web-search", "web_search"): ActionPolicy(
        connector_key="tavily-web-search",
        action="web_search",
        auth_kind="api_key",
        requires_snapshot=False,   # a read tool acts on a query, not a result
        requires_grant=False,      # api_key: no per-user OAuth grant
        egress_class="search_query",
        validate=_validate_tavily_search,
        required_scopes=(),
        is_read=True,
    ),
}


def scopes_satisfied(required: tuple, granted: Optional[str]) -> bool:
    """Return True if every required scope is present in the granted scope string.

    Matched case-insensitively by the trailing scope name so a fully-qualified
    grant (e.g. ``https://graph.microsoft.com/Mail.Send``) satisfies ``Mail.Send``.
    When the grant did not report scopes we cannot verify and return True (the
    provider call will still fail if the scope is truly missing).
    """
    if not required:
        return True
    if not granted:
        return True  # unknown -> cannot verify; provider enforces at call time
    have = {s.strip().rsplit("/", 1)[-1].lower() for s in str(granted).replace(",", " ").split()}
    return all(str(r).rsplit("/", 1)[-1].lower() in have for r in required)


def get_action_policy(connector_key: str, action: str) -> Optional[ActionPolicy]:
    """Return the typed policy for ``(connector_key, action)`` or ``None``.

    ``None`` means the action is not permitted; callers MUST fail closed.
    """
    return _POLICIES.get((str(connector_key or ""), str(action or "")))
