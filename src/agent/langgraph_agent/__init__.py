"""LangGraph-based text-to-SQL agent package.

Exports two graphs:
  ``build_graph``                 — full text-to-SQL pipeline (16 nodes)
  ``build_insights_eval_graph``   — standalone eval subgraph for the insights API
"""
from src.agent.langgraph_agent.graph import (
    build_graph,
    build_insights_eval_graph,
    run_eval,
)
from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState, InsightsState

__all__ = [
    "build_graph",
    "build_insights_eval_graph",
    "run_eval",
    "PromptLoader",
    "AgentState",
    "InsightsState",
]
