"""Drop system-schema noise (and duplicates) out of catalog datasets.

Some catalog harvesters — notably the external ``schema-modeler`` MCP server —
ingest the *whole* database, including the PostgreSQL ``information_schema`` views
and ``pg_catalog`` tables, and can list the same user table more than once. That
inflates the ``@`` table picker and ``#`` column autocomplete with objects nobody
queries. AdventureWorksDW, for example, was returning 141 "tables" that were only
37 real tables (64 information_schema views + 5 pg_catalog objects + every real
table listed twice).

These helpers operate purely on the item *name*, so they are safe for any catalog
source (MCP or DB) and any dialect. They:
  * drop the fixed set of ``information_schema`` view names, and
  * drop ``pg_*`` / ``_pg_*`` (pg_catalog) objects, and
  * de-duplicate by case-insensitive name, keeping the richest entry.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Standard SQL / PostgreSQL information_schema view names. These are schema-level
# system views, never real user tables, so it is safe to exclude them by name.
INFORMATION_SCHEMA_VIEWS = frozenset({
    "administrable_role_authorizations", "applicable_roles", "attributes",
    "character_sets", "check_constraint_routine_usage", "check_constraints",
    "collation_character_set_applicability", "collations", "column_column_usage",
    "column_domain_usage", "column_options", "column_privileges",
    "column_udt_usage", "columns", "constraint_column_usage",
    "constraint_table_usage", "data_type_privileges", "domain_constraints",
    "domain_udt_usage", "domains", "element_types", "enabled_roles",
    "foreign_data_wrapper_options", "foreign_data_wrappers",
    "foreign_server_options", "foreign_servers", "foreign_table_options",
    "foreign_tables", "information_schema_catalog_name", "key_column_usage",
    "parameters", "referential_constraints", "role_column_grants",
    "role_routine_grants", "role_table_grants", "role_udt_grants",
    "role_usage_grants", "routine_column_usage", "routine_privileges",
    "routine_routine_usage", "routine_sequence_usage", "routine_table_usage",
    "routines", "schemata", "sequences", "sql_features",
    "sql_implementation_info", "sql_parts", "sql_sizing", "table_constraints",
    "table_privileges", "tables", "transforms", "triggered_update_columns",
    "triggers", "udt_privileges", "usage_privileges", "user_defined_types",
    "user_mapping_options", "user_mappings", "view_column_usage",
    "view_routine_usage", "view_table_usage", "views",
})


def is_system_object(name: Any) -> bool:
    """True when *name* is an information_schema view or a pg_catalog object."""
    n = str(name or "").strip().lower()
    if not n:
        return False
    if n in INFORMATION_SCHEMA_VIEWS:
        return True
    return n.startswith("pg_") or n.startswith("_pg_")


def _table_score(item: Any) -> int:
    """Rank duplicate table entries so we keep the most informative one."""
    if not isinstance(item, dict):
        return 0
    score = 0
    try:
        score += int(item.get("col_count") or 0)
    except (TypeError, ValueError):
        pass
    if (item.get("description") or "").strip():
        score += 1
    return score


def filter_tables_rich(items: List[Any]) -> List[Dict[str, Any]]:
    """Remove system objects and de-duplicate ``{name, ...}`` table entries."""
    out: List[Dict[str, Any]] = []
    index: Dict[str, int] = {}
    for it in items or []:
        name = it.get("name") if isinstance(it, dict) else it
        if not name or is_system_object(name):
            continue
        key = str(name).strip().lower()
        if key not in index:
            index[key] = len(out)
            out.append(it if isinstance(it, dict) else {"name": name})
        elif _table_score(it) > _table_score(out[index[key]]):
            out[index[key]] = it
    return out


def filter_columns(items: List[Any]) -> List[Dict[str, Any]]:
    """Drop columns that belong to a system-schema object (by ``table`` field)."""
    return [
        it for it in (items or [])
        if isinstance(it, dict) and not is_system_object(it.get("table"))
    ]
