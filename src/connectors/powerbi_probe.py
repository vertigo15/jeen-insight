"""Bounded read-only DAX probes that read a column's distinct values.

The curated catalog describes tables and columns but holds no instance data, so
verifying that a user-supplied literal (``"Mountaiin 300"``) exists means asking
the model. These probes are the narrow, read-only way to do that: one column at
a time, always wrapped in ``TOPN``, never returning fact rows.

Two shapes, in cost order:

``distinct_values``  ``EVALUATE TOPN(n+1, VALUES('T'[C]), 'T'[C], ASC)`` — pulls
    the column's domain so matching can happen locally, which is what makes
    typo tolerance possible at all (a server-side ``CONTAINSSTRING`` for a
    misspelled word matches nothing). Fetching ``n+1`` doubles as the
    cardinality check: getting the extra row proves the domain is larger than
    the cap, so no separate ``DISTINCTCOUNT`` round trip is needed.

``contains_values``  ``CONTAINSSTRING`` over token prefixes — the fallback for
    columns too large to pull. Prefixes rather than whole tokens so a
    misspelling after the first few characters still matches.

Every result carries ``ok`` and ``complete`` separately, because the three
outcomes mean very different things to a caller deciding whether a value
exists: a failed call proves nothing, a truncated read proves nothing about
absence, and only a complete read licenses "that value is not in this column".
Collapsing them would turn a transient API error into a confident (and wrong)
"no such product".

Probes run with the caller's delegated token, so row-level security applies to
them exactly as it does to the real query. The one gap is time: results are
cached per grant, so an RLS rule changed inside Power BI is only picked up when
the entry expires. The cache TTL is what bounds that window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

from src.connectors.powerbi import PowerBiDaxClient

logger = logging.getLogger(__name__)

# Hard ceiling on any single probe, independent of caller-supplied limits. One
# row inside it is reserved for the "is there more?" marker.
MAX_PROBE_ROWS = 5000


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one probe.

    ``ok``        the call succeeded (a failure is not evidence of anything).
    ``complete``  every matching value was returned, so absence is meaningful.
    """

    values: Tuple[str, ...]
    complete: bool
    ok: bool

    @classmethod
    def failed(cls) -> "ProbeResult":
        return cls(values=(), complete=False, ok=False)


def escape_dax_string(value: object) -> str:
    """Escape a DAX string literal (``"`` is doubled)."""
    return str(value or "").replace('"', '""')


def escape_table_name(name: object) -> str:
    """Escape a single-quoted DAX table name (``'`` is doubled)."""
    return str(name or "").replace("'", "''")


def escape_column_name(name: object) -> str:
    """Escape a bracketed DAX column name (``]`` is doubled)."""
    return str(name or "").replace("]", "]]")


def column_ref(table: object, column: object) -> str:
    """Render a fully qualified ``'Table'[Column]`` reference."""
    return f"'{escape_table_name(table)}'[{escape_column_name(column)}]"


def build_distinct_values_dax(table: str, column: str, limit: int) -> str:
    ref = column_ref(table, column)
    return f"EVALUATE\nTOPN({int(limit)}, VALUES({ref}), {ref}, ASC)"


def build_contains_dax(table: str, column: str, fragments: Sequence[str], limit: int) -> str:
    ref = column_ref(table, column)
    predicate = " || ".join(
        f'CONTAINSSTRING({ref}, "{escape_dax_string(f)}")' for f in fragments
    )
    return (
        f"EVALUATE\nTOPN({int(limit)}, FILTER(VALUES({ref}), {predicate}), {ref}, ASC)"
    )


class PowerBiValueProbe:
    """Reads distinct column values from one Power BI dataset.

    *token_getter* is an awaitable returning a delegated access token. It is
    called per probe and the token is never retained, matching the execution
    node's contract that no token enters graph state.
    """

    def __init__(
        self,
        client: PowerBiDaxClient,
        token_getter: Callable[[], Awaitable[str]],
        identity_getter: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
    ) -> None:
        self._client = client
        self._token_getter = token_getter
        self._identity_getter = identity_getter

    async def authorize(self) -> Optional[str]:
        """Return who is reading, or None when they may not read at all.

        Callers must await this before touching any cached domain: a cache hit
        performs no authorization of its own, and a clarification answer ends
        the turn without ever reaching the execution node that would re-check.

        The string identifies the *grant*, not the application user, so that
        relinking the same account to a different Power BI identity starts from
        an empty cache instead of inheriting the previous identity's view.
        """
        if self._identity_getter is not None:
            return await self._identity_getter()
        return "" if await self._token_getter() else None

    async def distinct_values(self, table: str, column: str, *, limit: int) -> ProbeResult:
        """Read a column's distinct values, capped at *limit*."""
        capped = self._cap(limit)
        dax = build_distinct_values_dax(table, column, capped + 1)
        values, raw_count, ok = await self._run(dax, capped + 1)
        if not ok:
            return ProbeResult.failed()
        # Truncation is judged on the raw row count, before blanks are dropped,
        # so a blank row can never disguise a truncated read as a complete one.
        if raw_count > capped:
            return ProbeResult(values=tuple(values[:capped]), complete=False, ok=True)
        return ProbeResult(values=tuple(values), complete=True, ok=True)

    async def contains_values(
        self, table: str, column: str, fragments: Sequence[str], *, limit: int
    ) -> ProbeResult:
        """Read distinct values containing any of *fragments* (case-insensitive)."""
        usable = [f for f in fragments if f]
        if not usable:
            return ProbeResult(values=(), complete=True, ok=True)
        capped = self._cap(limit)
        dax = build_contains_dax(table, column, usable, capped + 1)
        values, raw_count, ok = await self._run(dax, capped + 1)
        if not ok:
            return ProbeResult.failed()
        if raw_count > capped:
            return ProbeResult(values=tuple(values[:capped]), complete=False, ok=True)
        return ProbeResult(values=tuple(values), complete=True, ok=True)

    @staticmethod
    def _cap(limit: int) -> int:
        """Clamp a caller limit, leaving room for the truncation marker row."""
        return max(1, min(int(limit), MAX_PROBE_ROWS - 1))

    async def _run(self, dax: str, max_rows: int) -> Tuple[List[str], int, bool]:
        """Return ``(values, raw_row_count, ok)`` for one probe query."""
        token = await self._token_getter()
        if not token:
            return [], 0, False
        result = await self._client.execute_dax(dax, token, max_rows=max_rows)
        if result.get("error"):
            logger.info(
                "PowerBiValueProbe: probe failed (%s) — %s",
                result.get("error_type"),
                str(result.get("error"))[:120],
            )
            return [], 0, False
        rows = result.get("rows") or []
        values: List[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            for cell in row.values():
                if cell is not None and str(cell).strip():
                    values.append(str(cell))
                break
        return values, len(rows), True


__all__ = [
    "MAX_PROBE_ROWS",
    "PowerBiValueProbe",
    "ProbeResult",
    "build_contains_dax",
    "build_distinct_values_dax",
    "column_ref",
    "escape_column_name",
    "escape_dax_string",
    "escape_table_name",
]
