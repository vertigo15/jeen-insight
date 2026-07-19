"""Post-result agent tool planner (Phase 3 — smallest safe agent slice).

After ``/api/query`` produces a result and a durable snapshot, this planner may
propose EXACTLY ONE write action — sending the current result by email — using
the EXISTING Microsoft Graph Mail connector against the fresh snapshot.

Security invariants (from the plan):
  * Authorization is NEVER derived from result data — only from the USER's own
    question text (explicit intent). A data cell, a model hallucination, or a
    connector/tool response can never cause an outbound action.
  * At most ONE proposal per turn.
  * The proposal is only a DRAFT. The server re-validates and hash-binds the
    approved params at preview and runs ONLY those stored params at execute
    (see :mod:`src.connectors.action_gate`). Nothing sends here.
  * Fails closed and SILENT: any missing flag/entitlement/grant/connector simply
    yields no proposal (the normal answer is returned unchanged).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

GRAPH_MAIL_KEY = "microsoft-graph-mail"
TAVILY_KEY = "tavily-web-search"
SLACK_KEY = "slack-message"
JIRA_KEY = "jira-issue"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Unambiguous send verbs (word-boundary matched so "gmail" != "mail").
_SEND_VERBS = ("send", "share", "forward", "mail")
# Mail-channel nouns; word-boundary matched.
_CHANNEL = ("email", "e-mail", "mail")
_SELF_REFS = ("to me", "to myself", "me a copy", "me the", "myself the", "my inbox")

# "email" as an imperative VERB (not the noun in "email address"): at the start of
# the request (optionally after a polite lead-in) or directly followed by an object.
_EMAIL_VERB_RE = re.compile(
    r"(?:^|\b(?:please|pls|kindly|can you|could you|would you)\s+)e-?mail\b"
    r"|\be-?mail\s+(?:this|these|it|me|us|them|the|a|report|results?|table|chart)\b",
    re.IGNORECASE,
)


def _has_word(q: str, word: str) -> bool:
    return re.search(r"\b" + re.escape(word) + r"\b", q, re.IGNORECASE) is not None


def detect_send_email_intent(
    question: str, *, self_address: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Return draft email params if (and only if) the USER explicitly asked to
    email/send the result; otherwise ``None``.

    Intent requires an explicit send verb (``send``/``share``/``forward``/``mail``
    or ``email`` used as a verb) together with a mail channel word or an email
    recipient — so a data question like "how many customers have a gmail email
    address?" never triggers an outbound action. Recipients are extracted from the
    question, or resolved to the caller for "email it to me"; when intent is clear
    but no recipient is given, an empty list is returned so the confirm card forces
    the user to enter one (intent preserved; nothing implied).
    """
    if not question or not question.strip():
        return None
    q = question.lower()
    has_email_verb = _EMAIL_VERB_RE.search(question) is not None
    has_send_verb = has_email_verb or any(_has_word(q, v) for v in _SEND_VERBS)
    if not has_send_verb:
        return None
    recipients: List[str] = _dedupe(_EMAIL_RE.findall(question))
    has_channel = any(_has_word(q, c) for c in _CHANNEL)
    if not (has_channel or recipients):
        return None
    if not recipients and self_address and any(s in q for s in _SELF_REFS):
        recipients = [self_address]
    return {"recipients": recipients}


# Explicit web-search signals in the USER's text. Conservative on purpose: a
# normal analytics question must never trigger an external web call.
_WEB_SIGNALS = (
    "search the web",
    "search online",
    "search the internet",
    "web search",
    "on the internet",
    "on the web",
    "look it up online",
    "look up online",
    "google it",
    "google for",
    "latest news",
    "online for",
)


def detect_web_search_intent(question: str) -> Optional[Dict[str, Any]]:
    """Return a draft web-search query when the USER explicitly asked to search the
    web/internet; otherwise ``None``.

    Authorization is independent of any data — it is derived solely from explicit
    intent in the user's question, so a result cell or prior tool output can never
    trigger an external call.
    """
    if not question or not question.strip():
        return None
    q = question.lower()
    if not any(sig in q for sig in _WEB_SIGNALS):
        return None
    return {"query": question.strip()[:400]}


# Slack: an explicit "post/share/send … slack" request. A channel token (#name)
# is extracted when present; otherwise the confirm card / config default supplies
# the destination (still allowlist-checked server-side).
_SLACK_CHANNEL_RE = re.compile(r"#([A-Za-z0-9][A-Za-z0-9._-]{0,78})")
_SLACK_VERBS = ("post", "send", "share", "drop", "message")


def detect_slack_post_intent(question: str) -> Optional[Dict[str, Any]]:
    """Return a draft Slack post when the USER explicitly asked to post/share the
    result to Slack; otherwise ``None`` (a plain analytics question never fires)."""
    if not question or not question.strip():
        return None
    q = question.lower()
    if not _has_word(q, "slack"):
        return None
    if not (any(_has_word(q, v) for v in _SLACK_VERBS) or "slack this" in q):
        return None
    m = _SLACK_CHANNEL_RE.search(question)
    return {"channel": ("#" + m.group(1)) if m else ""}


# Jira: an explicit "create/open/file … jira" request.
_JIRA_VERBS = ("create", "open", "file", "raise", "log", "make", "add")


def detect_jira_intent(question: str) -> Optional[Dict[str, Any]]:
    """Return a draft marker when the USER explicitly asked to create a Jira issue
    from the result; otherwise ``None``."""
    if not question or not question.strip():
        return None
    q = question.lower()
    if not _has_word(q, "jira"):
        return None
    if not (any(_has_word(q, v) for v in _JIRA_VERBS) or "jira this" in q):
        return None
    return {}


def _dedupe(items: List[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for it in items:
        k = it.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(it.strip())
    return out


def default_subject(question: str) -> str:
    """Server-rendered subject line (never model-authored, length-capped)."""
    q = " ".join((question or "").split())
    return (f"Jeen Insights: {q}" if q else "Jeen Insights result")[:200]


async def plan_post_result_action(
    *,
    result: Dict[str, Any],
    principal: Any,
    registry: Any,
    grants: Any,
    identities: Any,
    gate: Any,
    ensure_identity: Callable[[Any], Any],
) -> Optional[Dict[str, Any]]:
    """Propose at most one Graph Mail send for the current result, or return None.

    Returns the proposal dict (proposal_id + nonce + draft params) for the UI to
    render a confirm card, or ``None`` when no action should be proposed.
    """
    from src.connectors.action_gate import ActionError
    from src.security.app_flags import get_agent_tools_enabled, get_connectors_enabled

    handle = result.get("result_handle")
    if not handle:
        return None  # no durable snapshot -> nothing to authorize against

    # Both master switches, read fresh (immediate kill switch).
    if not (
        await get_connectors_enabled(use_cache=False)
        and await get_agent_tools_enabled(use_cache=False)
    ):
        return None

    if not getattr(principal, "is_entra", False):
        return None
    try:
        identity = await ensure_identity(principal)
    except Exception:  # noqa: BLE001 - non-SSO / tenant mismatch -> no proposal
        return None

    self_address = identity.get("upn") or getattr(principal, "email", None)
    intent = detect_send_email_intent(result.get("question") or "", self_address=self_address)
    if intent is None:
        return None

    # Resolve the (enabled) Graph Mail connector; skip silently if unavailable or
    # the user is not entitled / not connected.
    try:
        connectors = await registry.list_connectors()
    except Exception:  # noqa: BLE001
        return None
    connector = next(
        (c for c in connectors if c.get("key") == GRAPH_MAIL_KEY and c.get("is_enabled")),
        None,
    )
    if not connector:
        return None
    connector_id = connector["id"]

    grant_ids = connector.get("group_grants") or []
    allowed, _reason = await identities.can_use_connector(
        identity["id"], connector_id, group_grant_ids=grant_ids
    )
    if not allowed:
        return None
    grant = await grants.get_grant(identity["id"], connector_id)
    if not grant or grant.get("status") != "active":
        return None

    draft = {
        "recipients": intent["recipients"],
        "subject": default_subject(result.get("question") or ""),
    }
    try:
        proposal = await gate.propose(
            owner_user_id=principal.user_id,
            identity_id=identity["id"],
            connector_id=connector_id,
            action="send_email",
            snapshot_id=handle,
            params=draft,
            origin="agent",
        )
    except ActionError as exc:
        logger.debug("tool planner proposal rejected: %s", exc)
        return None

    _append_trace(result, "Proposed emailing this result (awaiting your confirmation)")
    proposal["kind"] = "confirm"
    proposal["prompt"] = "Send this result by email?"
    return proposal


async def plan_read_action(
    *,
    result: Dict[str, Any],
    principal: Any,
    registry: Any,
    identities: Any,
    gate: Any,
    ensure_identity: Callable[[Any], Any],
) -> Optional[Dict[str, Any]]:
    """Propose at most one Tavily web_search when the user explicitly asked to
    search the web, or return None. No snapshot / no per-user grant (api_key).

    On confirm the UI runs preview -> execute -> continue; the continuation feeds
    the fenced, size-capped search data back with TOOLS DISABLED.
    """
    from src.connectors.action_gate import ActionError
    from src.security.app_flags import get_agent_tools_enabled, get_connectors_enabled

    if not (
        await get_connectors_enabled(use_cache=False)
        and await get_agent_tools_enabled(use_cache=False)
    ):
        return None
    if not getattr(principal, "is_entra", False):
        return None
    intent = detect_web_search_intent(result.get("question") or "")
    if intent is None:
        return None
    try:
        identity = await ensure_identity(principal)
    except Exception:  # noqa: BLE001
        return None

    try:
        connectors = await registry.list_connectors()
    except Exception:  # noqa: BLE001
        return None
    connector = next(
        (c for c in connectors if c.get("key") == TAVILY_KEY and c.get("is_enabled")),
        None,
    )
    if not connector:
        return None
    connector_id = connector["id"]

    grant_ids = connector.get("group_grants") or []
    allowed, _reason = await identities.can_use_connector(
        identity["id"], connector_id, group_grant_ids=grant_ids
    )
    if not allowed:
        return None

    draft = {"query": intent["query"], "max_results": 5}
    try:
        proposal = await gate.propose(
            owner_user_id=principal.user_id,
            identity_id=identity["id"],
            connector_id=connector_id,
            action="web_search",
            params=draft,
            origin="agent",
        )
    except ActionError as exc:
        logger.debug("read planner proposal rejected: %s", exc)
        return None

    _append_trace(result, "Proposed a web search (awaiting your confirmation)")
    proposal["kind"] = "read"
    proposal["prompt"] = "Search the web to answer this?"
    return proposal


def _first_cfg(connector: Dict[str, Any], key: str) -> str:
    """First value of a connector-config allowlist (e.g. a default channel/project),
    or '' when none is configured. Only a convenience default for the confirm card;
    the server re-validates the destination against the full allowlist."""
    cfg = (connector.get("current_version") or {}).get("config") or {}
    vals = cfg.get(key) or []
    if isinstance(vals, str):
        vals = [x for x in vals.replace(";", ",").split(",")]
    vals = [str(x).strip() for x in vals if str(x).strip()]
    return vals[0] if vals else ""


async def _propose_oauth_write(
    *,
    result: Dict[str, Any],
    principal: Any,
    registry: Any,
    grants: Any,
    identities: Any,
    gate: Any,
    ensure_identity: Callable[[Any], Any],
    connector_key: str,
    action: str,
    draft_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    prompt: str,
    trace: str,
) -> Optional[Dict[str, Any]]:
    """Shared body for snapshot-bound OAuth write planners (Slack, Jira).

    Fails closed and silent on any missing flag/identity/connector/entitlement/
    grant. The draft params are only a prefill for the confirm card; the server
    re-validates + hash-binds the approved params at preview and runs ONLY those.
    """
    from src.connectors.action_gate import ActionError
    from src.security.app_flags import get_agent_tools_enabled, get_connectors_enabled

    handle = result.get("result_handle")
    if not handle:
        return None
    if not (
        await get_connectors_enabled(use_cache=False)
        and await get_agent_tools_enabled(use_cache=False)
    ):
        return None
    if not getattr(principal, "is_entra", False):
        return None
    try:
        identity = await ensure_identity(principal)
    except Exception:  # noqa: BLE001
        return None
    try:
        connectors = await registry.list_connectors()
    except Exception:  # noqa: BLE001
        return None
    connector = next(
        (c for c in connectors if c.get("key") == connector_key and c.get("is_enabled")),
        None,
    )
    if not connector:
        return None
    connector_id = connector["id"]

    grant_ids = connector.get("group_grants") or []
    allowed, _reason = await identities.can_use_connector(
        identity["id"], connector_id, group_grant_ids=grant_ids
    )
    if not allowed:
        return None
    grant = await grants.get_grant(identity["id"], connector_id)
    if not grant or grant.get("status") != "active":
        return None

    try:
        draft = draft_fn(connector)
    except Exception:  # noqa: BLE001
        draft = {}
    try:
        proposal = await gate.propose(
            owner_user_id=principal.user_id,
            identity_id=identity["id"],
            connector_id=connector_id,
            action=action,
            snapshot_id=handle,
            params=draft,
            origin="agent",
        )
    except ActionError as exc:
        logger.debug("write planner (%s) rejected: %s", action, exc)
        return None

    _append_trace(result, trace)
    proposal["kind"] = "confirm"
    proposal["prompt"] = prompt
    return proposal


async def plan_slack_action(
    *,
    result: Dict[str, Any],
    principal: Any,
    registry: Any,
    grants: Any,
    identities: Any,
    gate: Any,
    ensure_identity: Callable[[Any], Any],
) -> Optional[Dict[str, Any]]:
    """Propose at most one Slack post of the current result, or return None."""
    intent = detect_slack_post_intent(result.get("question") or "")
    if intent is None:
        return None

    def draft_fn(connector: Dict[str, Any]) -> Dict[str, Any]:
        return {"channel": intent.get("channel") or _first_cfg(connector, "allowed_channels")}

    return await _propose_oauth_write(
        result=result, principal=principal, registry=registry, grants=grants,
        identities=identities, gate=gate, ensure_identity=ensure_identity,
        connector_key=SLACK_KEY, action="post_message", draft_fn=draft_fn,
        prompt="Post this result to Slack?",
        trace="Proposed posting this result to Slack (awaiting your confirmation)",
    )


async def plan_jira_action(
    *,
    result: Dict[str, Any],
    principal: Any,
    registry: Any,
    grants: Any,
    identities: Any,
    gate: Any,
    ensure_identity: Callable[[Any], Any],
) -> Optional[Dict[str, Any]]:
    """Propose at most one Jira issue from the current result, or return None."""
    intent = detect_jira_intent(result.get("question") or "")
    if intent is None:
        return None
    question = result.get("question") or ""

    def draft_fn(connector: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "project_key": _first_cfg(connector, "allowed_projects"),
            "issue_type": _first_cfg(connector, "allowed_issue_types") or "Task",
            "summary": default_subject(question)[:255],
        }

    return await _propose_oauth_write(
        result=result, principal=principal, registry=registry, grants=grants,
        identities=identities, gate=gate, ensure_identity=ensure_identity,
        connector_key=JIRA_KEY, action="create_issue", draft_fn=draft_fn,
        prompt="Create a Jira issue from this result?",
        trace="Proposed creating a Jira issue (awaiting your confirmation)",
    )


def _append_trace(result: Dict[str, Any], detail: str) -> None:
    trace = result.get("trace")
    if not isinstance(trace, list):
        trace = []
        result["trace"] = trace
    trace.append(
        {
            "node": "tool_planner",
            "type": "tool",
            "icon": "🔧",
            "detail": detail,
            "elapsed_ms": 0,
        }
    )
