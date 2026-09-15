"""ML skills for Jeen Insights.

A *skill* is a contract, not a prompt: a fixed parameter model, declared
guards, a pinned engine and one shared result envelope. The LLM's only job is
to pick a skill and fill its parameters; every number in the result is
produced by the deterministic engines in :mod:`src.analysis.engines`.

Package layout
--------------
contracts.py   Versioned pydantic models: SkillSpec, SeriesRequest, per-skill
               params, GuardResult, ResultEnvelope. No ML dependencies.
series.py      pandas series preparation: calendar reindex, missingness mask,
               fill policy, seasonal-period detection.
guards.py      Pure guard functions run on the fetched series.
engines/       statsforecast/statsmodels-backed skill implementations.
sql_builder.py sqlglot AST builder for the portable aggregation query.
runner.py      AnalysisRunner protocol + local (dev) and HTTP sandbox runners.
"""
