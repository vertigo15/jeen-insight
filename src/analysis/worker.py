"""Child-process worker for :class:`LocalSubprocessRunner` and the sandbox.

Reads exactly one JSON request from stdin::

    {"skill": ..., "params": {...}, "series": {"columns": [...], "rows": [...]},
     "override_guards": false, "context": {...}, "memory_mb": 1024}

and writes exactly one JSON :class:`RunOutcome` to stdout. Resource limits are
applied *before* the heavy imports so a runaway fit is capped by the kernel,
not by good intentions. Nothing in the request is ever executed as code.
"""

from __future__ import annotations

import json
import sys


def _apply_limits(memory_mb: int) -> None:
    try:
        import resource

        limit = int(memory_mb) * 1024 * 1024
        for name in ("RLIMIT_AS", "RLIMIT_DATA"):
            res = getattr(resource, name, None)
            if res is None:
                continue
            try:
                soft, hard = resource.getrlimit(res)
                new_hard = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
                resource.setrlimit(res, (min(limit, new_hard), new_hard))
            except (ValueError, OSError):
                continue
        # No core dumps from a crashed fit.
        core = getattr(resource, "RLIMIT_CORE", None)
        if core is not None:
            try:
                resource.setrlimit(core, (0, 0))
            except (ValueError, OSError):
                pass
    except ImportError:
        pass


def main() -> int:
    raw = sys.stdin.read()
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stdout.write(json.dumps({"status": "error", "error": f"bad request: {exc}"}))
        return 2

    _apply_limits(int(request.get("memory_mb") or 1024))

    from src.analysis.runner import execute_skill  # noqa: PLC0415 — after limits

    outcome = execute_skill(
        str(request.get("skill") or ""),
        dict(request.get("params") or {}),
        dict(request.get("series") or {}),
        override_guards=bool(request.get("override_guards")),
        context=dict(request.get("context") or {}),
    )
    sys.stdout.write(json.dumps(outcome.to_json(), default=str))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
