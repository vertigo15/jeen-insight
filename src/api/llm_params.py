"""Centralised LLM tuning per feature.

Values previously inlined across `main.py`. Kept as a dataclass per feature
so callers say `params=AUTOCOMPLETE_PARAMS` instead of magic numbers, and
tweaks land in one place.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LlmParams:
    temperature: float
    max_tokens: int


# Tier-3 autocomplete suggestions: short, low-creativity, JSON-only.
AUTOCOMPLETE_PARAMS = LlmParams(temperature=0.2, max_tokens=200)

# Main text-to-SQL agent.
QUERY_PARAMS = LlmParams(temperature=0.3, max_tokens=2048)

# Chart edits via natural language (chart chat). Low creativity, JSON-only.
EDIT_CHART_PARAMS = LlmParams(temperature=0.2, max_tokens=4096)

# Compact v2 chart operations never contain chart data or full ECharts config.
EDIT_CHART_OPERATIONS_PARAMS = LlmParams(temperature=0.1, max_tokens=800)

# Initial chart generation: a touch more creative for layout/colour choices.
GENERATE_CHART_PARAMS = LlmParams(temperature=0.5, max_tokens=4096)

# "Enhance" pass over an existing chart config.
ENHANCE_CHART_PARAMS = LlmParams(temperature=0.3, max_tokens=4096)
