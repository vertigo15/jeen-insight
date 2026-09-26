"""The shared ``/api/query`` route calls ``agent.process_question(**kwargs)`` on
whichever agent ``resolve_agent`` returns — SQL or DAX — with one fixed keyword
set. Both agents must therefore accept the same keywords, or every Power BI
question fails with ``TypeError`` before any graph runs (this happened when
``analysis_enabled`` was added to the SQL agent only).
"""

from __future__ import annotations

import inspect

from src.agent.dax_insights_agent import DaxInsightsAgent
from src.agent.jeen_insights_agent import JeenInsightsAgent


def _keyword_params(fn) -> set[str]:
    return {
        name
        for name, p in inspect.signature(fn).parameters.items()
        if name != "self" and p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)
    }


def test_sql_and_dax_agents_accept_the_same_process_question_keywords():
    sql_params = _keyword_params(JeenInsightsAgent.process_question)
    dax_params = _keyword_params(DaxInsightsAgent.process_question)
    assert sql_params == dax_params, (
        f"only SQL: {sorted(sql_params - dax_params)}; only DAX: {sorted(dax_params - sql_params)}"
    )


def test_route_keywords_are_accepted_by_both_agents():
    # Mirrors the call in src/api/routes/query.py.
    route_kwargs = {
        "question", "session_id", "user_context", "limit", "temperature",
        "eval_analytics", "llm_timeout", "progress_callback", "analysis_enabled",
        "filter_choices", "partial_callback", "answer_callback",
    }
    for agent_cls in (JeenInsightsAgent, DaxInsightsAgent):
        missing = route_kwargs - _keyword_params(agent_cls.process_question)
        assert not missing, f"{agent_cls.__name__}.process_question lacks {sorted(missing)}"
