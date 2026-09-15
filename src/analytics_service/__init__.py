"""jeen-insights-analytics — the ML-skills sandbox service.

A small FastAPI app that runs *our* pinned skill modules (``src.analysis``)
with validated parameters and nothing else. It never evaluates model-authored
code; that single property is what separates it from a code interpreter.

Isolation is layered: the container has no network egress, no credentials,
a read-only filesystem and a per-run tmpfs; every run executes in a child
process with a hard wall-clock kill and an address-space limit; requests are
authenticated with the same HMAC token scheme the UI→API boundary uses, bound
to this service's own audience; payloads are capped by size and row count.
"""
