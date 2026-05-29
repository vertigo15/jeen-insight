"""LangGraph-based analytics agent for Jeen Insights.

Currently exposes the *insights eval* subgraph, which handles the
post-execution phase: intent evaluation, result summarisation, key
insights extraction, and follow-up question generation.

The full text-to-SQL pipeline (memory → router → sql_gen → execution →
validation → eval → output) will live here as well; the remaining nodes
are added incrementally — only the eval node is wired today.

Usage
-----
    from src.agent.langgraph_agent import build_insights_eval_graph, run_eval

    graph = build_insights_eval_graph(llm_service, prompt_cache)
    state = await run_eval(
        graph,
        question="...",
        sql="SELECT ...",
        results=[{...}, ...],
        row_count=42,
    )
    # state["follow_up_questions"] -> List[str]
"""
from .graph import build_insights_eval_graph, run_eval

__all__ = ["build_insights_eval_graph", "run_eval"]
