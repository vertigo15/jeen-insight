"""Power BI ``executeQueries`` client for read-only DAX.

Wraps the **legacy JSON** endpoint
``POST /v1.0/myorg/groups/{workspaceId}/datasets/{datasetId}/executeQueries`` and
returns the standard Jeen ``{columns, rows, row_count}`` shape so the shared
post-data pipeline (insights, charts, memory) treats DAX results exactly like
SQL results.

Verified constraints this client enforces / handles (see the plan):
  * One query per call, one result table per query.
  * No ``queryTimeout`` / ``resultSetRowCountLimit`` params on the legacy API —
    the row cap is pushed into the DAX with ``TOPN`` (see
    :func:`src.connectors.dax_safety.apply_topn_cap`); the engine still caps at
    100k rows / 1M values / ~15 MB.
  * **HTTP 200 can carry errors** (e.g. "More than one result table",
    "More than N rows"): the client inspects the top-level ``error``, each
    ``results[i].error`` and each ``results[i].tables[j].error`` and rejects
    partial / truncated results instead of returning silent garbage.
  * Column keys come back as ``Table[Column]`` (qualified) or ``[Alias]``
    (renamed/created); they are normalised to friendly display names.

The bearer token is passed **per request** and never stored on the client or in
graph state.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from src.connectors import egress
from src.connectors.dax_safety import apply_topn_cap, is_read_only_dax

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.powerbi.com"
# Legacy executeQueries can return up to ~15 MB; give the egress buffer headroom.
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_DEFAULT_TIMEOUT = 60.0

# Substrings Power BI uses in 200-with-error payloads for the hard engine limits.
_LIMIT_HINTS = ("more than", "exceeded", "too many", "maximum")


def _display_name(key: str) -> str:
    """Reduce a ``Table[Column]`` / ``[Alias]`` key to its bracketed name."""
    if not isinstance(key, str):
        return str(key)
    lb = key.rfind("[")
    rb = key.rfind("]")
    if lb != -1 and rb > lb:
        inner = key[lb + 1 : rb].replace("]]", "]").strip()
        return inner or key
    return key


def _normalize_table(table: Dict[str, Any]) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Map a Power BI result table to ``(columns, rows)`` with friendly keys.

    Preserves column order from the first rows and disambiguates display-name
    collisions (e.g. ``'A'[X]`` and ``'B'[X]``) by keeping the full key.
    """
    raw_rows = table.get("rows") or []
    ordered_keys: List[str] = []
    seen: set = set()
    for row in raw_rows[:50]:
        if isinstance(row, dict):
            for k in row.keys():
                if k not in seen:
                    seen.add(k)
                    ordered_keys.append(k)

    display_counts: Dict[str, int] = {}
    for k in ordered_keys:
        d = _display_name(k)
        display_counts[d] = display_counts.get(d, 0) + 1

    key_to_display: Dict[str, str] = {}
    for k in ordered_keys:
        d = _display_name(k)
        key_to_display[k] = k if display_counts[d] > 1 else d

    columns = [key_to_display[k] for k in ordered_keys]
    rows: List[Dict[str, Any]] = []
    for row in raw_rows:
        if isinstance(row, dict):
            rows.append({key_to_display.get(k, k): v for k, v in row.items()})
        else:  # pragma: no cover - PBI always returns dict rows
            rows.append(row)
    return columns, rows


def _error(
    message: str,
    *,
    error_type: str,
    http_status: Optional[int] = None,
    pbi_error_code: Optional[str] = None,
    error_location: Optional[str] = None,
    retry_after: Optional[float] = None,
) -> Dict[str, Any]:
    return {
        "error": message,
        "error_type": error_type,
        "http_status": http_status,
        "pbi_error_code": pbi_error_code,
        "pbi_error_message": message,
        "error_location": error_location,
        "retry_after": retry_after,
        "is_partial": error_type in ("partial_result", "limit_exceeded"),
        "columns": [],
        "rows": [],
        "row_count": 0,
    }


def _extract_error(obj: Any) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(code, message)`` from a Power BI ``error`` object."""
    if not isinstance(obj, dict):
        return None, None
    err = obj.get("error")
    if not isinstance(err, dict):
        return None, None
    code = err.get("code")
    message = err.get("message") or ""
    details = err.get("pbi.error") or err.get("details")
    if not message and isinstance(details, dict):
        message = details.get("code") or ""
    return (str(code) if code else None), (str(message) if message else None)


class PowerBiDaxClient:
    """Executes read-only DAX against one Power BI dataset via ``executeQueries``."""

    def __init__(
        self,
        *,
        workspace_id: str,
        dataset_id: str,
        api_base: str = DEFAULT_API_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        if not workspace_id:
            raise ValueError("PowerBiDaxClient requires a workspace_id")
        if not dataset_id:
            raise ValueError("PowerBiDaxClient requires a dataset_id")
        self.workspace_id = workspace_id
        self.dataset_id = dataset_id
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self.timeout = timeout
        parts = urlsplit(self.api_base)
        self._origin = f"{parts.scheme}://{parts.netloc}".lower()

    @property
    def execute_url(self) -> str:
        return (
            f"{self.api_base}/v1.0/myorg/groups/{self.workspace_id}"
            f"/datasets/{self.dataset_id}/executeQueries"
        )

    async def execute_dax(
        self,
        dax: str,
        access_token: str,
        *,
        max_rows: int = 10000,
        impersonated_user: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run *dax* and return ``{columns, rows, row_count}`` or an error dict.

        The error dict carries ``error_type`` / ``http_status`` /
        ``pbi_error_code`` / ``error_location`` so the DAX feedback router can map
        the failure to a repair strategy without re-parsing HTTP.
        """
        ok, reason = is_read_only_dax(dax)
        if not ok:
            logger.warning("PowerBiDaxClient: blocked non-read-only DAX — %s", reason)
            return _error(reason, error_type="read_only_blocked")
        if not access_token:
            return _error(
                "No Power BI access token was provided for this request.",
                error_type="auth",
            )

        capped = apply_topn_cap(dax, max_rows)
        query: Dict[str, Any] = {"query": capped}
        payload: Dict[str, Any] = {
            "queries": [query],
            "serializerSettings": {"includeNulls": True},
        }
        if impersonated_user:
            payload["impersonatedUserName"] = impersonated_user

        try:
            resp = await egress.request(
                "POST",
                self.execute_url,
                allowed_origins=(self._origin,),
                json=payload,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
                max_bytes=_MAX_RESPONSE_BYTES,
            )
        except egress.ResponseTooLarge as exc:
            return _error(
                "The Power BI result set is too large. Add filters or a smaller "
                "date range so the result fits within the API limit.",
                error_type="limit_exceeded",
            )
        except egress.EgressError as exc:  # policy violation — should never happen
            logger.error("PowerBiDaxClient: egress policy error: %s", exc)
            return _error(str(exc), error_type="transport")
        except Exception as exc:  # noqa: BLE001 — network/transport failure
            logger.warning("PowerBiDaxClient: transport error: %s", exc)
            return _error(
                "Could not reach Power BI. Please try again.",
                error_type="transport",
            )

        return self._parse_response(resp.status_code, resp, max_rows)

    # ── response handling ────────────────────────────────────────────────────
    def _parse_response(self, status: int, resp: Any, max_rows: int) -> Dict[str, Any]:
        if status != 200:
            return self._parse_http_error(status, resp)

        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return _error(
                "Power BI returned an unreadable response.",
                error_type="execution_error",
                http_status=200,
            )

        # Top-level error can appear even on 200.
        code, message = _extract_error(body)
        if message:
            return self._classify_200_error(code, message, location="results")

        results = body.get("results")
        if not isinstance(results, list) or not results:
            return _error(
                "Power BI returned no result set.",
                error_type="empty",
                http_status=200,
            )
        # Legacy endpoint: exactly one query -> exactly one result.
        result0 = results[0] if isinstance(results[0], dict) else {}
        code, message = _extract_error(result0)
        if message:
            return self._classify_200_error(code, message, location="results[0]")

        tables = result0.get("tables")
        if not isinstance(tables, list) or not tables:
            return _error(
                "Power BI returned a result with no table.",
                error_type="empty",
                http_status=200,
            )
        if len(tables) > 1:
            return _error(
                "The query produced more than one result table. Rewrite it as a "
                "single EVALUATE returning one table.",
                error_type="partial_result",
                http_status=200,
                error_location="results[0].tables",
            )

        table0 = tables[0] if isinstance(tables[0], dict) else {}
        code, message = _extract_error(table0)
        if message:
            return self._classify_200_error(code, message, location="results[0].tables[0]")

        columns, rows = _normalize_table(table0)
        row_count = len(rows)
        truncated = row_count >= max_rows if max_rows and max_rows > 0 else False
        logger.info(
            "PowerBiDaxClient: %d row(s) x %d col(s)%s",
            row_count,
            len(columns),
            " (truncated at cap)" if truncated else "",
        )
        return {
            "columns": columns,
            "rows": rows,
            "row_count": row_count,
            "is_partial": bool(truncated),
            "truncated": bool(truncated),
            "http_status": 200,
        }

    def _classify_200_error(
        self, code: Optional[str], message: str, *, location: str
    ) -> Dict[str, Any]:
        low = (message or "").lower()
        if any(hint in low for hint in _LIMIT_HINTS):
            etype = "limit_exceeded"
        else:
            etype = "execution_error"
        return _error(
            message,
            error_type=etype,
            http_status=200,
            pbi_error_code=code,
            error_location=location,
        )

    def _parse_http_error(self, status: int, resp: Any) -> Dict[str, Any]:
        code: Optional[str] = None
        message: Optional[str] = None
        try:
            body = resp.json()
            code, message = _extract_error(body)
        except Exception:  # noqa: BLE001
            message = None
        message = message or self._default_http_message(status)

        if status == 400:
            etype = "bad_request"
        elif status == 401:
            etype = "auth"
        elif status == 403:
            etype = "forbidden"
        elif status == 404:
            etype = "not_found"
        elif status == 429:
            etype = "throttled"
        elif 500 <= status < 600:
            etype = "service"
        else:
            etype = "execution_error"

        retry_after: Optional[float] = None
        if status == 429:
            try:
                retry_after = float(resp.headers.get("Retry-After"))
            except (TypeError, ValueError):
                retry_after = None

        return _error(
            message,
            error_type=etype,
            http_status=status,
            pbi_error_code=code,
            retry_after=retry_after,
        )

    @staticmethod
    def _default_http_message(status: int) -> str:
        if status == 400:
            return "Power BI rejected the DAX query (400 Bad Request)."
        if status == 401:
            return "Power BI authentication failed (401). The token may be expired."
        if status == 403:
            return (
                "Access to this Power BI dataset was denied (403). You need "
                "workspace access plus dataset Read + Build permission, and the "
                "tenant 'Dataset Execute Queries REST API' setting must be on."
            )
        if status == 404:
            return "The Power BI workspace or dataset was not found (404)."
        if status == 429:
            return "Power BI is throttling requests (429). Please retry shortly."
        if 500 <= status < 600:
            return f"Power BI service error ({status}). Please try again."
        return f"Power BI returned HTTP {status}."


__all__ = ["PowerBiDaxClient"]
