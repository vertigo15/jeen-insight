"""Centralised logging configuration for both the FastAPI API and the Flask UI.

This replaces the two ad-hoc ``logging.basicConfig`` calls that previously lived
in ``src/api/app_factory.py`` and ``src/ui_app.py``. ``basicConfig`` only takes
effect when the root logger has no handlers yet, so under gunicorn/uvicorn (which
install their own root handler first) it silently no-ops — the custom format and
``LOG_LEVEL`` never applied. :func:`configure_logging` uses ``dictConfig``, which
always applies, and additionally:

* emits structured JSON in production (readable console text in dev),
* stamps every record with a per-request correlation id (see
  :class:`RequestIdFilter` / :data:`REQUEST_ID_HEADER`), and
* tames noisy third-party loggers (httpx, asyncpg, msal, uvicorn.access, ...).

Call :func:`configure_logging` once per process, as early as possible.

This module deliberately reads its inputs from the environment rather than the
``Settings`` model so the (credential-free) UI process can configure logging
without constructing the full API settings object.
"""

from __future__ import annotations

import contextvars
import datetime as _dt
import json
import logging
import logging.config
import os
import re
import sys
import uuid
from typing import Optional

# Header used to carry the correlation id across the UI -> API hop and echo it
# back to the caller. Kept here so every layer references a single constant.
REQUEST_ID_HEADER = "X-Request-ID"

# An inbound request id is attacker-controlled: constrain it to a safe charset
# and length so it cannot forge log lines (CRLF) or inject response headers.
_REQUEST_ID_STRIP = re.compile(r"[^A-Za-z0-9._-]")
_MAX_REQUEST_ID_LEN = 128

# Extra keys whose values are redacted from JSON logs even if code passes them
# via ``logger.*(..., extra={...})``. Defence in depth against secret leakage.
# Segment-aware (splits on _/-/./space) so real secrets like ``access_token`` or
# ``api_key`` are redacted while metrics like ``total_tokens`` and ids like
# ``source_key`` are not.
_SECRET_SEGMENTS = frozenset({
    "password", "passwd", "pwd", "secret", "token", "authorization",
    "cookie", "credential", "credentials",
})
_SECRET_COMPOUND_RE = re.compile(
    r"apikey|accesstoken|refreshtoken|bearertoken|idtoken|privatekey|"
    r"secretkey|clientsecret|sessionkey|encryptionkey"
)


def _is_secret_key(key: str) -> bool:
    """True when a log-record extra key names a secret and must be redacted."""
    low = key.lower()
    if any(seg in _SECRET_SEGMENTS for seg in re.split(r"[^a-z0-9]+", low)):
        return True
    return bool(_SECRET_COMPOUND_RE.search(re.sub(r"[^a-z0-9]+", "", low)))

_VALID_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}

# Standard ``LogRecord`` attributes; anything else on a record is treated as a
# structured "extra" (from ``logger.info(..., extra={...})``) and folded into the
# JSON payload.
_RESERVED_RECORD_KEYS = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    "request_id",
}


# ── Correlation id (request-scoped) ──────────────────────────────────────────
_request_id_ctx: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "jeen_request_id", default=None
)


def new_request_id() -> str:
    """Return a fresh short correlation id."""
    return uuid.uuid4().hex


def _clean_request_id(value: Optional[str]) -> Optional[str]:
    """Sanitise an inbound correlation id, or return ``None`` if unusable."""
    if not value:
        return None
    cleaned = _REQUEST_ID_STRIP.sub("", value.strip())[:_MAX_REQUEST_ID_LEN]
    return cleaned or None


def get_request_id() -> Optional[str]:
    """Return the correlation id bound to the current context, if any."""
    return _request_id_ctx.get()


def bind_request_id(value: Optional[str]) -> "contextvars.Token":
    """Bind ``value`` (or a fresh id when falsy/unsafe) as the correlation id.

    Returns the context token so the caller can restore the previous value with
    :func:`reset_request_id` once the request finishes.
    """
    return _request_id_ctx.set(_clean_request_id(value) or new_request_id())


def reset_request_id(token: "contextvars.Token") -> None:
    """Restore the correlation id to its previous value (best effort, idempotent).

    Catches the errors a stray double-reset can raise — ``RuntimeError`` when a
    token is reused, ``ValueError``/``LookupError`` when it belongs to another
    context (e.g. a reused worker thread) — so cleanup can never crash a request.
    """
    try:
        _request_id_ctx.reset(token)
    except (RuntimeError, ValueError, LookupError):
        _request_id_ctx.set(None)


# ── Filters / formatters ─────────────────────────────────────────────────────
class RequestIdFilter(logging.Filter):
    """Attach the current correlation id to every record as ``request_id``."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        return True


class JsonLogFormatter(logging.Formatter):
    """Render a record as a single-line JSON object for log aggregators."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        # Fold in structured extras passed via ``logger.*(..., extra={...})``.
        for key, val in record.__dict__.items():
            if key in _RESERVED_RECORD_KEYS or key.startswith("_"):
                continue
            if _is_secret_key(key):
                payload[key] = "<redacted>"
                continue
            try:
                json.dumps(val)
                payload[key] = val
            except (TypeError, ValueError):
                payload[key] = repr(val)
        return json.dumps(payload, default=str)


_CONSOLE_FORMAT = (
    "%(asctime)s - %(name)s - %(levelname)s - [%(request_id)s] - %(message)s"
)


def _resolve_level(value: Optional[str]) -> str:
    """Return a valid upper-case level name, defaulting to INFO on garbage.

    Kept lenient here (unlike the strict ``Settings`` validator) so a typo in
    the UI process cannot take the whole UI down at import time.
    """
    up = str(value or "INFO").strip().upper()
    if up not in _VALID_LEVELS:
        sys.stderr.write(
            f"[logging_config] invalid log level {value!r}; falling back to INFO\n"
        )
        return "INFO"
    return up


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "t")


def configure_logging(
    level: Optional[str] = None,
    fmt: Optional[str] = None,
    dev_mode: Optional[bool] = None,
) -> None:
    """Configure process-wide logging. Idempotent — safe to call more than once.

    Args:
        level: log level name; falls back to ``$LOG_LEVEL`` then ``INFO``.
        fmt: ``"json"``, ``"console"`` or ``"auto"``; falls back to
            ``$LOG_FORMAT`` then ``"auto"``. ``"auto"`` picks ``console`` in dev
            mode and ``json`` otherwise.
        dev_mode: overrides ``$JEEN_DEV_MODE`` for the ``auto`` decision.
    """
    resolved_level = _resolve_level(level or os.getenv("LOG_LEVEL", "INFO"))
    resolved_fmt = (fmt or os.getenv("LOG_FORMAT", "auto")).strip().lower()
    if resolved_fmt not in ("auto", "json", "console"):
        resolved_fmt = "auto"
    if resolved_fmt == "auto":
        is_dev = dev_mode if dev_mode is not None else _env_bool("JEEN_DEV_MODE", True)
        resolved_fmt = "console" if is_dev else "json"
    formatter = "json" if resolved_fmt == "json" else "console"

    logging.config.dictConfig({
        "version": 1,
        # Keep module-level loggers created before this call alive.
        "disable_existing_loggers": False,
        "filters": {
            "request_id": {"()": f"{__name__}.RequestIdFilter"},
        },
        "formatters": {
            "json": {"()": f"{__name__}.JsonLogFormatter"},
            "console": {"format": _CONSOLE_FORMAT},
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": formatter,
                "filters": ["request_id"],
            },
        },
        "root": {"level": resolved_level, "handlers": ["default"]},
        "loggers": {
            # Route the server's own loggers through the single root handler so
            # every line shares one format. Empty handler list + propagate=True
            # strips any handler uvicorn/gunicorn installed first.
            "uvicorn": {"level": resolved_level, "handlers": [], "propagate": True},
            "uvicorn.error": {"level": resolved_level, "handlers": [], "propagate": True},
            "uvicorn.access": {"level": "WARNING", "handlers": [], "propagate": True},
            "gunicorn.error": {"level": resolved_level, "handlers": [], "propagate": True},
            "gunicorn.access": {"level": "WARNING", "handlers": [], "propagate": True},
            # Chatty third-party libraries — quiet unless the root is DEBUG.
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
            "asyncpg": {"level": "WARNING"},
            "msal": {"level": "WARNING"},
            "urllib3": {"level": "WARNING"},
        },
    })
