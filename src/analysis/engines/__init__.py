"""Skill engines. Each module exposes ``run(params, sf, ctx) -> ResultEnvelope``.

Engines never see the database, the catalog or the user's text: they receive
validated parameters and a prepared :class:`~src.analysis.series.SeriesFrame`
and return the shared envelope. That is what lets the same module run in the
API process (dev) and inside the sandbox container (prod).
"""
