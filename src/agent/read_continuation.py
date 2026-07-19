"""Response-only continuation for read tools (Phase 5).

After a read tool (e.g. Tavily) executes and its untrusted result is captured in
an encrypted TTL artifact, the model must be re-entered ONCE to compose the final
answer from that data. This is the plan's "response-only continuation": a single
LLM turn with the artifact fenced + size-capped and TOOLS DISABLED — the model
cannot chain into another tool call, and the fenced data can never authorize an
action (a data cell/web result is information, never an instruction).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.connectors.tool_result_service import fence_tool_data

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _default_prompt(name: str) -> str:
    return (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def build_continuation_messages(
    question: str,
    fenced_data: str,
    *,
    system_template: Optional[str] = None,
    user_template: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Construct the (system, user) messages for the tools-disabled answer turn."""
    fallback = _default_prompt("read_continuation_user")
    template = user_template or fallback
    try:
        user_content = template.format(question=question or "", fenced_data=fenced_data)
    except (IndexError, KeyError, ValueError):
        # Legacy DB prompt versions can predate save-time placeholder validation.
        # A malformed administrator template must not prevent a response-only
        # turn from answering a successfully fetched read artifact.
        logger.warning(
            "read_continuation: malformed user template; falling back to packaged default",
            exc_info=True,
        )
        user_content = fallback.format(question=question or "", fenced_data=fenced_data)
    return [
        {"role": "system", "content": system_template or _default_prompt("read_continuation_system")},
        {"role": "user", "content": user_content},
    ]


async def continue_read(
    *,
    proposal_id: str,
    artifact_id: str,
    question: str,
    owner_user_id: str,
    session_id: Optional[str],
    tool_results: Any,
    llm: Any,
    prompt_cache: Any = None,
) -> Dict[str, Any]:
    """Load the artifact (single-consume), then produce a final answer with tools
    DISABLED. Raises ValueError when the artifact is unavailable/expired/consumed.
    """
    consumed = await tool_results.consume(
        artifact_id, owner_user_id=owner_user_id, session_id=session_id
    )
    if not consumed:
        # Post-confirm terminal failure: the read result is gone; do NOT silently
        # answer without it (that could fabricate). The caller surfaces this.
        raise ValueError("The search result is no longer available. Please run the search again.")

    system_template = None
    user_template = None
    model_override = None
    if prompt_cache is not None:
        try:
            system_template = await prompt_cache.get_content("read_continuation_system")
            model_override = await prompt_cache.get_model_override("read_continuation_system")
        except Exception:  # noqa: BLE001
            logger.warning("read_continuation: system prompt cache unavailable", exc_info=True)
        try:
            user_template = await prompt_cache.get_content("read_continuation_user")
        except Exception:  # noqa: BLE001
            logger.warning("read_continuation: user prompt cache unavailable", exc_info=True)

    fenced = fence_tool_data(consumed["payload"])
    messages = build_continuation_messages(
        question,
        fenced,
        system_template=system_template,
        user_template=user_template,
    )
    # TOOLS DISABLED: tools=None so the model cannot chain another tool call.
    resp = await llm.generate(
        messages,
        tools=None,
        temperature=0.2,
        model_override=model_override,
    )
    answer = resp.get("content") if isinstance(resp, dict) else str(resp)
    return {
        "proposal_id": proposal_id,
        "answer": answer or "",
        "tools_disabled": True,
    }
