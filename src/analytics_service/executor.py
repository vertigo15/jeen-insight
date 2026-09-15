"""Child-process execution with a hard wall clock and memory cap.

The service imports the engines once at startup (the numba JIT is the slow
part), then forks a child per run so the child starts warm. The child applies
``RLIMIT_AS`` before working and writes one JSON outcome to a pipe; the parent
waits ``timeout_seconds`` and kills the child if it is still running. A crash
or kill becomes a plain ``{"status": "error"}`` — the service process itself
never dies with a bad fit.

``fork`` is used on Linux (the container). Elsewhere ``spawn`` is the safe
choice and pays the import cost per run; that only matters for local tests.
"""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutorConfig:
    timeout_seconds: int = 30
    memory_mb: int = 1024
    start_method: Optional[str] = None  # fork on Linux, spawn elsewhere


def _apply_limits(memory_mb: int) -> None:
    try:
        import resource

        limit = int(memory_mb) * 1024 * 1024
        for name in ("RLIMIT_AS", "RLIMIT_DATA"):
            res = getattr(resource, name, None)
            if res is None:
                continue
            try:
                _soft, hard = resource.getrlimit(res)
                new_hard = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
                resource.setrlimit(res, (min(limit, new_hard), new_hard))
            except (ValueError, OSError):
                continue
        core = getattr(resource, "RLIMIT_CORE", None)
        if core is not None:
            try:
                resource.setrlimit(core, (0, 0))
            except (ValueError, OSError):
                pass
    except ImportError:
        pass


def _child(conn, request: Dict[str, Any], memory_mb: int, target: Optional[Callable] = None) -> None:
    """Runs in the child: limits first, then the one entry point, then one message."""
    _apply_limits(memory_mb)
    try:
        fn = target
        if fn is None:
            from src.analysis.runner import execute_skill  # noqa: PLC0415

            fn = execute_skill
        outcome = fn(
            request["skill"], request["params"], request["series"],
            override_guards=bool(request.get("override_guards")), context=dict(request.get("context") or {}),
        )
        payload = outcome.to_json() if hasattr(outcome, "to_json") else dict(outcome)
    except MemoryError:
        payload = {"status": "error", "error": "analysis exceeded its memory limit"}
    except Exception as exc:  # noqa: BLE001
        payload = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    try:
        conn.send_bytes(json.dumps(payload, default=str).encode("utf-8"))
    finally:
        conn.close()


class ForkExecutor:
    """One child process per run, killed at ``timeout_seconds``."""

    def __init__(self, config: ExecutorConfig = ExecutorConfig(), *, target: Optional[Callable] = None) -> None:
        self.config = config
        self._target = target  # test seam: an alternative execute_skill
        method = config.start_method or ("fork" if sys.platform.startswith("linux") else "spawn")
        self._ctx = mp.get_context(method)
        self._warm()

    def _warm(self) -> None:
        """Import the engines in the parent so forked children start warm."""
        try:
            import importlib

            import src.analysis.runner  # noqa: F401
            from src.analysis.contracts import SKILLS  # noqa: PLC0415

            for spec in SKILLS.values():
                importlib.import_module(spec.engine)
            from statsforecast.models import AutoARIMA, AutoETS  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            logger.warning("executor: engine warm-up incomplete: %s", exc)

    def engine_versions(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for name in ("statsforecast", "statsmodels", "pandas", "numpy", "scipy", "ruptures", "sklearn"):
            try:
                out[name] = __import__(name).__version__
            except Exception:  # noqa: BLE001
                out[name] = "unavailable"
        return out

    def run_sync(self, skill: str, params: Dict[str, Any], series: Dict[str, Any], *,
                 override_guards: bool = False, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        request = {"skill": skill, "params": params, "series": series,
                   "override_guards": override_guards, "context": context or {}}
        parent_conn, child_conn = self._ctx.Pipe(duplex=False)
        proc = self._ctx.Process(target=_child, args=(child_conn, request, self.config.memory_mb, self._target), daemon=True)
        t0 = time.monotonic()
        proc.start()
        child_conn.close()
        payload: Optional[Dict[str, Any]] = None
        try:
            if parent_conn.poll(self.config.timeout_seconds):
                try:
                    payload = json.loads(parent_conn.recv_bytes().decode("utf-8"))
                except EOFError:
                    payload = None  # child died without a message; report its exit code below
                except ValueError as exc:
                    payload = {"status": "error", "error": f"analysis worker returned unreadable output: {exc}"}
            proc.join(timeout=max(0.0, self.config.timeout_seconds - (time.monotonic() - t0)) + 1.0)
        finally:
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=5)
            parent_conn.close()
        if payload is None:
            if proc.exitcode is None or proc.exitcode == -9:
                return {"status": "error", "error": f"analysis timed out after {self.config.timeout_seconds}s"}
            return {"status": "error", "error": f"analysis worker exited {proc.exitcode} without a result"}
        return payload

    async def run(self, skill, params, series, *, override_guards=False, context=None) -> Dict[str, Any]:
        import asyncio

        return await asyncio.to_thread(
            self.run_sync, skill, params, series, override_guards=override_guards, context=context,
        )
