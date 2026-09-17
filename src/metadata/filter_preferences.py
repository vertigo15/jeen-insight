"""Remembered filter-grounding choices, per user and connection.

When the grounder asks "which field did you mean?" or "which value?", the
answer is stored here so the question is never repeated: for the same literal
(``"mosco" → dim_dealer.city``), for the same *role* (this user's "city" means
the dealer's city, reused when they later ask about Paris), or as "any of
these fields" for a literal. Preferences are advisory — the grounder
re-applies today's governance before honouring one.

The store degrades to nothing when the metadata DB is unavailable; remembering
a choice must never fail a question.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from src.metadata.value_index import normalize

logger = logging.getLogger(__name__)

_MAX_PREFERENCES = 200
_MAX_TEXT = 255


class FilterPreferenceStore:
    """Persistence for ``filter_choices`` over the metadata pool."""

    def __init__(self, pool: Any = None, *, pool_getter: Optional[Any] = None) -> None:
        self.pool = pool
        self._pool_getter = pool_getter

    @property
    def available(self) -> bool:
        return self.pool is not None or self._pool_getter is not None

    async def _pool(self) -> Any:
        if self.pool is None and self._pool_getter is not None:
            self.pool = await self._pool_getter()
        return self.pool

    async def load(self, user_id: str, source_key: str) -> List[Dict[str, Any]]:
        """All remembered choices for one user on one connection, newest first."""
        if not self.available or not user_id or not source_key:
            return []
        try:
            async with (await self._pool()).acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT literal_norm, role_column, table_name, column_name, any_of, chosen_value
                    FROM insights_filter_preferences
                    WHERE user_id = $1 AND source_key = $2
                    ORDER BY updated_at DESC
                    LIMIT $3
                    """,
                    str(user_id), str(source_key), _MAX_PREFERENCES,
                )
        except Exception as exc:  # noqa: BLE001 - memory is optional
            logger.info("filter_preferences: load failed (%s)", type(exc).__name__)
            return []
        return [
            {
                "literal": row["literal_norm"] or "",
                "role": row["role_column"] or "",
                "table": row["table_name"],
                "column": row["column_name"],
                "any": bool(row["any_of"]),
                "value": row["chosen_value"],
            }
            for row in rows
        ]

    async def save(self, user_id: str, source_key: str, choices: Sequence[Dict[str, Any]]) -> int:
        """Upsert the user's answers; returns the number of rows written."""
        if not self.available or not user_id or not source_key:
            return 0
        rows = list(preference_rows(choices))
        if not rows:
            return 0
        try:
            async with (await self._pool()).acquire() as conn:
                for row in rows:
                    await conn.execute(
                        """
                        INSERT INTO insights_filter_preferences
                            (user_id, source_key, literal_norm, role_column, table_name, column_name, any_of, chosen_value, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                        ON CONFLICT (user_id, source_key, literal_norm, role_column) DO UPDATE
                            SET table_name = EXCLUDED.table_name,
                                column_name = EXCLUDED.column_name,
                                any_of = EXCLUDED.any_of,
                                chosen_value = EXCLUDED.chosen_value,
                                updated_at = NOW()
                        """,
                        str(user_id), str(source_key), row["literal"], row["role"],
                        row["table"], row["column"], row["any"], row["value"],
                    )
        except Exception as exc:  # noqa: BLE001
            logger.info("filter_preferences: save failed (%s)", type(exc).__name__)
            return 0
        return len(rows)


def preference_rows(choices: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalise raw ``filter_choices`` into the rows the table stores.

    A column choice yields the literal-level row and refreshes the role-level
    row for that column name; a value choice keeps the value with the literal;
    "any of these fields" is literal-level with no column.
    """
    rows: Dict[tuple, Dict[str, Any]] = {}
    for choice in choices or []:
        if not isinstance(choice, dict):
            continue
        literal = normalize(choice.get("literal"))[:_MAX_TEXT]
        table = str(choice.get("table") or "").strip().lower()[:_MAX_TEXT] or None
        column = str(choice.get("column") or "").strip().lower()[:_MAX_TEXT] or None
        value = str(choice["value"])[:2000] if choice.get("value") is not None else None
        any_of = bool(choice.get("any"))
        if not literal and not (column and not any_of):
            continue
        if any_of:
            rows[(literal, "")] = {"literal": literal, "role": "", "table": None, "column": None, "any": True, "value": None}
            continue
        if not column:
            continue
        rows[(literal, column)] = {"literal": literal, "role": column, "table": table, "column": column,
                                   "any": False, "value": value}
        if literal:
            rows[("", column)] = {"literal": "", "role": column, "table": table, "column": column,
                                  "any": False, "value": None}
    return list(rows.values())


_store: Optional[FilterPreferenceStore] = None


def get_filter_preference_store() -> FilterPreferenceStore:
    global _store
    if _store is None:
        from src.metadata.metadata_db import get_metadata_pool  # noqa: PLC0415

        _store = FilterPreferenceStore(pool_getter=get_metadata_pool)
    return _store


def reset_for_tests(store: Optional[FilterPreferenceStore] = None) -> None:
    global _store
    _store = store


__all__ = ["FilterPreferenceStore", "get_filter_preference_store", "preference_rows", "reset_for_tests"]
