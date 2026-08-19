"""DAX static validation + local repair (no sqlglot; the engine is authoritative).

dax_static_validate   Sync node. Runs the scope-aware lexer/linter (read-only /
                      shape gate, DEFINE MEASURE/VAR-only, balanced delimiters,
                      banned MDX/DMV/DDL), resolves identifiers against the
                      catalog (``'Table'[Column]`` vs ``[Measure]``), enforces DLP
                      over referenced columns + measure lineage, and checks that a
                      detail-grain query carries a bounded ``TOPN``.
dax_repair            Async LLM node. Focused local repair of a query that failed
                      static validation, BEFORE it is ever executed.

Errors are split into two channels:
  * ``dax_validation_error`` — blocking (governance / read-only / banned token /
    static-repair budget exhausted) → route to response_formatter.
  * ``dax_repairable_error`` + ``dax_lint_errors`` — feeds the local repair loop.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set

from src.agent.langgraph_agent.nodes.validation import _DLP_PATTERNS
from src.agent.langgraph_agent_dax.nodes.dax_gen import _extract_dax
from src.agent.langgraph_agent_dax.prompt_loader import DaxPromptLoader
from src.agent.langgraph_agent_dax.state import DaxAgentState
from src.agent.llm_service import LangChainLlmService
from src.connectors.dax_safety import banned_token, is_read_only_dax, lex_dax
from src.agent.token_usage import merge_usage
from src.tools.dax_tool import RunDaxTool

logger = logging.getLogger(__name__)

# Max lexer/linter-driven local repairs before the query is declared unfixable.
DAX_MAX_STATIC_REPAIRS = 2

_TOPN_RE = re.compile(r"\bTOPN\s*\(", re.IGNORECASE)
# DEFINE-block identifier that declares a measure name: MEASURE 'Table'[Name] = …
_DEFINE_MEASURE_RE = re.compile(
    r"\bMEASURE\b[^\[\n]*\[([^\]]+)\]", re.IGNORECASE
)


# Governed-column matching, DAX flavour. The shared patterns were written for
# SQL identifiers and are underscore-style ("social_security"), but a tabular
# model names its columns for humans — "Social Security Number" — so applying
# them unchanged would wave through exactly the columns they exist to protect.
_DLP_SEPARATOR_RE = re.compile(r"[_\-]+")
_DLP_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def build_dax_dlp_regex(extra_columns: Optional[List[str]]) -> "re.Pattern[str]":
    """Compile the governed-column policy with separators made flexible.

    Operator-configured names get the same treatment as the built-ins: someone
    who writes ``home_address`` in the settings means the column, not that exact
    punctuation, and the model very likely spells it "Home Address".
    """
    patterns = [p.replace("_", r"[\s_-]*") for p in _DLP_PATTERNS]
    for col in extra_columns or []:
        words = [w for w in re.split(r"[^0-9A-Za-z]+", _DLP_CAMEL_RE.sub(" ", str(col or ""))) if w]
        if words:
            patterns.append(r"\b" + r"[\s_-]*".join(re.escape(w) for w in words) + r"\b")
    return re.compile("|".join(patterns), re.IGNORECASE)


def is_governed_name(dlp_re: "re.Pattern[str]", name: object) -> bool:
    """True when a column name is governed, however it is punctuated.

    ``SocialSecurityNumber``, ``social_security_number`` and "Social Security
    Number" are one column to a reader and must be one column to the policy, so
    the name is reduced to spaced words before matching rather than requiring
    the patterns to anticipate every casing convention.
    """
    raw = str(name or "")
    spaced = _DLP_SEPARATOR_RE.sub(" ", raw)
    return bool(dlp_re.search(raw) or dlp_re.search(_DLP_CAMEL_RE.sub(" ", spaced)))


def _static_repairs(state: DaxAgentState) -> int:
    return int((state.get("repair_attempts_by_category") or {}).get("static", 0))


def make_dax_static_validate(
    enabled: bool = True,
    require_catalog: bool = True,
    dlp_enabled: bool = True,
    dlp_governed_columns: Optional[List[str]] = None,
):
    """Return a sync ``dax_static_validate`` node."""
    dlp_re = build_dax_dlp_regex(dlp_governed_columns)

    def dax_static_validate(state: DaxAgentState) -> Dict[str, Any]:
        dax = state.get("generated_dax") or ""
        base: Dict[str, Any] = {
            "dax_lint_errors": [],
            "dax_validation_error": None,
            "dax_repairable_error": None,
            "dlp_blocked": False,
            "governance_error": None,
            "identifiers_used": [],
            "defined_measures": [],
            "resolved_symbols": {},
            "governed_lineage": [],
        }
        if not enabled or not dax.strip():
            return base

        if require_catalog and not (state.get("known_tables") or []):
            base["dax_validation_error"] = (
                "No catalog metadata is available for this dataset, so the DAX "
                "cannot be safely validated."
            )
            return base

        # 1) Hard read-only / shape gate (banned tokens, balance, single EVALUATE).
        ok, reason = is_read_only_dax(dax)
        lex = lex_dax(dax)
        if not ok:
            # A banned MDX/DMV/DDL token is a governance block (don't repair-loop).
            if banned_token(lex):
                base["dax_validation_error"] = (
                    "This query contains a disallowed keyword and was blocked."
                )
                logger.warning("dax_static_validate: BLOCKED banned token — %s", reason)
                return base
            base["dax_lint_errors"] = [reason]
            return _repairable_or_exhausted(state, base, [reason])

        # 2) DEFINE MEASURE/VAR-only gate.
        bad_kinds = [k for k in lex.define_kinds if k in ("COLUMN", "TABLE")]
        lint: List[str] = []
        if bad_kinds:
            lint.append(
                f"DEFINE may only contain MEASURE and VAR; found DEFINE {bad_kinds[0]}."
            )

        defined_measures = {m.strip().lower() for m in _DEFINE_MEASURE_RE.findall(dax)}

        # 3) Symbol resolution against the catalog (conservative).
        known_tables: Set[str] = {t.lower() for t in (state.get("known_tables") or [])}
        table_columns: Dict[str, Set[str]] = {
            t.lower(): {c.lower() for c in cols}
            for t, cols in (state.get("table_columns") or {}).items()
        }
        known_measures: Set[str] = {m.lower() for m in (state.get("known_measures") or [])}
        known_columns: Set[str] = {c.lower() for c in (state.get("known_columns") or [])}

        identifiers_used: List[Dict[str, Any]] = []
        governed: List[str] = []
        for ident in lex.identifiers:
            identifiers_used.append(
                {"table": ident.table, "name": ident.name, "qualified": ident.qualified}
            )
            name_l = ident.name.strip().lower()
            if ident.qualified and ident.table:
                tl = ident.table.strip().lower()
                if known_tables and tl not in known_tables:
                    lint.append(
                        f"Unknown table '{ident.table}' in '{ident.table}'[{ident.name}]."
                    )
                    continue
                cols = table_columns.get(tl)
                if cols and name_l not in cols:
                    lint.append(
                        f"Unknown column '{ident.name}' on table '{ident.table}'."
                    )
                if is_governed_name(dlp_re, ident.name):
                    governed.append(f"{ident.table}[{ident.name}]")
            else:
                # Unqualified [Name]: a measure, a defined measure, or a
                # current-row-context column. Only flag when catalog is populated
                # AND it matches none of them (avoids false positives on aliases).
                is_known = (
                    name_l in known_measures
                    or name_l in defined_measures
                    or name_l in known_columns
                )
                if (known_measures or known_columns) and not is_known:
                    lint.append(
                        f"Unknown measure or column '[{ident.name}]'. Measures are "
                        f"'[Name]'; columns are ''Table''[Column]."
                    )
                if is_governed_name(dlp_re, ident.name):
                    governed.append(f"[{ident.name}]")

        base["identifiers_used"] = identifiers_used
        base["defined_measures"] = sorted(defined_measures)

        # 4) DLP over columns + measure lineage (blocking).
        if dlp_enabled and governed:
            base["governed_lineage"] = governed
            base["dlp_blocked"] = True
            base["governance_error"] = (
                "Query blocked by data governance policy: it references a governed "
                f"field ({governed[0]})."
            )
            logger.warning("dax_static_validate: DLP BLOCKED — %s", governed[0])
            return base

        # 5) TOPN present for detail grain.
        if state.get("plan_grain") == "detail" and not _TOPN_RE.search(lex.tokens):
            lint.append(
                "Detail-grain query must use a bounded TOPN(<n>, <table>, <orderBy>) "
                "with an explicit ORDER BY."
            )

        if lint:
            return _repairable_or_exhausted(state, base, lint)

        logger.info("dax_static_validate: DAX passed static validation")
        return base

    return dax_static_validate


def _repairable_or_exhausted(
    state: DaxAgentState, base: Dict[str, Any], lint: List[str]
) -> Dict[str, Any]:
    """Emit a repairable error, or promote to blocking once repairs are spent."""
    base["dax_lint_errors"] = lint
    if _static_repairs(state) >= DAX_MAX_STATIC_REPAIRS:
        base["dax_validation_error"] = (
            "The generated DAX could not be made valid after repeated repair "
            "attempts: " + "; ".join(lint[:3])
        )
        logger.info("dax_static_validate: static repair budget exhausted")
    else:
        base["dax_repairable_error"] = "; ".join(lint)
        logger.info("dax_static_validate: repairable — %s", lint[0][:100])
    return base


# ── dax_repair ─────────────────────────────────────────────────────────────────


def make_dax_repair(llm: LangChainLlmService, prompt_loader: DaxPromptLoader):
    """Return an async ``dax_repair`` node (focused pre-execution repair)."""

    async def dax_repair(state: DaxAgentState) -> Dict[str, Any]:
        from src.api.llm_params import QUERY_PARAMS

        question = state.get("question", "")
        dax = state.get("generated_dax") or ""
        lint_errors = state.get("dax_lint_errors") or []
        error_context = state.get("dax_repairable_error") or state.get("error_context") or ""
        plan = state.get("query_plan")
        plan_text = json.dumps(plan, indent=2, default=str) if plan else "No plan."
        display_name = state.get("connection_display_name", "")

        prompt = await prompt_loader.arender(
            "dax_repair",
            question=question,
            dax=dax,
            error_context=error_context,
            lint_errors="\n".join(f"- {e}" for e in lint_errors) or "- (see context)",
            plan=plan_text,
        )
        model_override = await prompt_loader.model_override_for("dax_repair")

        dax_tool = RunDaxTool(
            display_name,
            dataset_id=state.get("dataset_id") or "",
            workspace_id=state.get("workspace_id") or "",
        )

        t0 = time.monotonic()
        response = await llm.generate(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Return the corrected DAX via run_dax."},
            ],
            temperature=0.0,
            max_tokens=QUERY_PARAMS.max_tokens,
            tools=[dax_tool.get_schema()],
            model_override=model_override,
            timeout=state.get("llm_timeout_seconds"),
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        usage = response.get("usage") or {}

        repaired = _extract_dax(response)
        counts = dict(state.get("repair_attempts_by_category") or {})
        counts["static"] = counts.get("static", 0) + 1

        updates: Dict[str, Any] = {
            "llm_call_count": (state.get("llm_call_count") or 0) + 1,
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + latency_ms,
            "token_usage": merge_usage(state.get("token_usage") or {}, usage),
            "repair_attempts_by_category": counts,
            "dax_repairable_error": None,
            "dax_lint_errors": [],
            "node_prompts": {**(state.get("node_prompts") or {}), "dax_repair": prompt},
        }
        if repaired:
            updates["generated_dax"] = repaired
            updates["generated_sql"] = repaired
            logger.info("dax_repair: produced repaired DAX (len=%d)", len(repaired))
        else:
            # No usable repair — keep the prior DAX; validate will exhaust and block.
            logger.warning("dax_repair: model returned no DAX; keeping prior query")
        return updates

    return dax_repair


__all__ = ["make_dax_static_validate", "make_dax_repair", "DAX_MAX_STATIC_REPAIRS"]
