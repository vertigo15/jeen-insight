"""Grade a generated SQL (or DAX) statement against a knowledge-pair gold.

Called by tests/e2e/live/sqlEquivalence.js with one JSON document on stdin and
answers with one JSON document on stdout. Reuses the project's sqlglot and
dialect mapping so the grader parses SQL the way the validation node does.

Tiers, strongest first (the reported ``tier`` is the first that holds):

* ``exact``            normalized text is identical (case, whitespace, quoting,
                       comments, trailing semicolon ignored)
* ``equivalent``       sqlglot canonical form is identical after lower-casing
                       identifiers and anonymizing table/column aliases
* ``structural``       same tables, same aggregates over the same columns, same
                       GROUP BY expressions, same WHERE literals and columns,
                       same LIMIT
* ``execution_match``  (opt-in: ``gold_dsn`` given, Postgres) the gold SQL run
                       read-only returns the same rows the app returned
* ``mismatch``         none of the above; ``diff`` carries a sqlglot edit script

Input::

    {"gold": str, "generated": str, "dialect": "postgres" | null,
     "language": "sql" | "dax",
     "gold_dsn": str | null, "app_rows": [ {col: val} ] | null,
     "app_columns": [str] | null, "app_truncated": bool}

Output::

    {"tier": str, "verdicts": {...}, "overlap": float, "normalized": {...},
     "canonical": {...}, "fingerprint": {...}, "diff": [str], "execution": {...},
     "error": str | null}
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

# ``ungraded`` = one side could not be parsed, so no tier could be computed (never a pass, never a product fail).
TIER_ORDER = ["exact", "equivalent", "structural", "execution_match", "mismatch", "ungraded"]


# ── Text normalization ────────────────────────────────────────────────────────


def strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*[\s\S]*?\*/", " ", sql)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def normalize_text(sql: str) -> str:
    text = strip_comments(str(sql or ""))
    text = text.replace('"', "").replace("`", "")
    text = re.sub(r"\s+", " ", text).strip().rstrip(";").strip()
    text = re.sub(r"\s*([(),])\s*", r"\1", text)
    return text.lower()


# ── sqlglot canonical form ────────────────────────────────────────────────────


def _parse(sql: str, dialect: Optional[str]) -> exp.Expression:
    return sqlglot.parse_one(strip_comments(sql), read=dialect)


def _normalized_tree(sql: str, dialect: Optional[str]) -> exp.Expression:
    """Parse, lower-case identifiers, drop quoting and unwrap type casts so
    "SalesAmount", salesamount, "salesamount" and CAST(salesamount AS DECIMAL)
    all read the same. Casts are a dialect necessity (Postgres ``money`` must
    be cast before arithmetic), not a semantic difference."""
    tree = normalize_identifiers(_parse(sql, dialect), dialect=dialect)
    for identifier in tree.find_all(exp.Identifier):
        identifier.set("quoted", False)
    tree = tree.transform(lambda node: node.this if isinstance(node, (exp.Cast, exp.TryCast)) else node)
    return tree


def _alias_bare_projections(tree: exp.Expression) -> exp.Expression:
    """Give every un-aliased projection an alias so `d.calendaryear` and
    `d.calendaryear AS year` anonymize to the same positional name."""
    counter = 0
    for select in tree.find_all(exp.Select):
        projections = []
        for item in select.expressions:
            if isinstance(item, (exp.Alias, exp.Star)) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
                projections.append(item)
                continue
            counter += 1
            projections.append(exp.alias_(item, f"_bare{counter}"))
        select.set("expressions", projections)
    return tree


def _anonymize_aliases(tree: exp.Expression) -> exp.Expression:
    """Rename table aliases to _t1.. and projection aliases to _c1.. in order of
    appearance, rewriting references, so two queries that differ only in alias
    names canonicalize to the same text."""
    tree = _alias_bare_projections(tree)
    table_map: Dict[str, str] = {}
    for i, table in enumerate(tree.find_all(exp.Table, exp.Subquery), start=1):
        alias = table.alias
        if alias and alias not in table_map:
            table_map[alias.lower()] = f"_t{i}"
    for node in tree.find_all(exp.Table, exp.Subquery):
        alias = node.alias
        if alias and alias.lower() in table_map:
            node.set("alias", exp.TableAlias(this=exp.to_identifier(table_map[alias.lower()])))
    for column in tree.find_all(exp.Column):
        if column.table and column.table.lower() in table_map:
            column.set("table", exp.to_identifier(table_map[column.table.lower()]))

    col_map: Dict[str, str] = {}
    for i, alias_node in enumerate(tree.find_all(exp.Alias), start=1):
        name = alias_node.alias
        if name and name.lower() not in col_map and not isinstance(alias_node.parent, (exp.Table, exp.Subquery)):
            col_map[name.lower()] = f"_c{i}"
    for alias_node in tree.find_all(exp.Alias):
        name = alias_node.alias
        if name and name.lower() in col_map:
            alias_node.set("alias", exp.to_identifier(col_map[name.lower()]))
    for column in tree.find_all(exp.Column):
        if not column.table and column.name.lower() in col_map:
            column.set("this", exp.to_identifier(col_map[column.name.lower()]))
    return tree


def canonical_sql(sql: str, dialect: Optional[str]) -> str:
    tree = _anonymize_aliases(_normalized_tree(sql, dialect))
    return tree.sql(dialect=dialect, normalize=True, comments=False, pretty=False)


# ── Structural fingerprint ────────────────────────────────────────────────────


def _col_name(column: exp.Column) -> str:
    return column.name.lower()


def _expr_text(node: exp.Expression) -> str:
    """Expression text with columns unqualified and lower-cased (aliases and
    table qualifiers must not affect the fingerprint)."""
    clone = node.copy()
    for column in clone.find_all(exp.Column):
        column.set("table", None)
    return re.sub(r"\s+", " ", clone.sql(normalize=True, comments=False)).lower()


def fingerprint(sql: str, dialect: Optional[str]) -> Dict[str, Any]:
    tree = _normalized_tree(sql, dialect)

    cte_names = {cte.alias.lower() for cte in tree.find_all(exp.CTE) if cte.alias}
    tables: Set[str] = set()
    for table in tree.find_all(exp.Table):
        if table.name and table.name.lower() not in cte_names:
            tables.add(table.name.lower())

    aggregates: Set[str] = set()
    for agg in tree.find_all(exp.AggFunc):
        aggregates.add(_expr_text(agg))

    group_by: Set[str] = set()
    for group in tree.find_all(exp.Group):
        for item in group.expressions:
            group_by.add(_expr_text(item))

    filter_literals: Set[str] = set()
    filter_columns: Set[str] = set()
    for where in tree.find_all(exp.Where):
        for literal in where.find_all(exp.Literal):
            filter_literals.add(str(literal.this).lower())
        for column in where.find_all(exp.Column):
            filter_columns.add(_col_name(column))

    # Whole predicate trees (columns unqualified), so `a AND b` vs `a OR b`, `=` vs `<>`
    # and `IN (…)` vs `=` are differences, not just "same literals".
    predicates: Set[str] = set()
    for where in tree.find_all(exp.Where):
        predicates.add(_expr_text(where.this))
    for having in tree.find_all(exp.Having):
        predicates.add("having " + _expr_text(having.this))

    joins: Set[str] = set()
    for join in tree.find_all(exp.Join):
        kind = " ".join(x for x in (join.side, join.kind) if x).lower() or "inner"
        on = join.args.get("on")
        joins.add(f"{kind} join {join.this.name.lower() if isinstance(join.this, exp.Table) else 'subquery'} on {_expr_text(on) if on is not None else 'none'}")

    distinct = any(select.args.get("distinct") is not None for select in tree.find_all(exp.Select))

    limit: Optional[str] = None
    lim = tree.find(exp.Limit)
    if lim is not None and lim.expression is not None:
        limit = lim.expression.sql().lower()
    fetch = tree.find(exp.Fetch)
    if limit is None and fetch is not None and fetch.args.get("count") is not None:
        limit = fetch.args["count"].sql().lower()

    has_order = tree.find(exp.Order) is not None
    has_window = tree.find(exp.Window) is not None
    has_union = tree.find(exp.Union) is not None

    return {
        "tables": sorted(tables),
        "aggregates": sorted(aggregates),
        "group_by": sorted(group_by),
        "predicates": sorted(predicates),
        "joins": sorted(joins),
        "distinct": distinct,
        "filter_literals": sorted(filter_literals),
        "filter_columns": sorted(filter_columns),
        "limit": limit,
        "has_order": has_order,
        "has_window": has_window,
        "has_union": has_union,
    }


def structural_equal(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Same relational skeleton: tables, join graph, predicates, grouping,
    aggregates, DISTINCT and LIMIT. Projection aliases and ORDER BY may differ."""
    return (
        a["tables"] == b["tables"]
        and a["joins"] == b["joins"]
        and a["aggregates"] == b["aggregates"]
        and a["group_by"] == b["group_by"]
        and a["predicates"] == b["predicates"]
        and a["distinct"] == b["distinct"]
        and a["has_union"] == b["has_union"]
        and a["limit"] == b["limit"]
    )


def fingerprint_delta(gold: Dict[str, Any], gen: Dict[str, Any]) -> Dict[str, Any]:
    """What the generated statement has that the gold does not, and vice versa —
    the readable explanation behind a structural mismatch."""
    delta: Dict[str, Any] = {}
    for key in ("tables", "joins", "aggregates", "group_by", "predicates", "filter_literals"):
        only_gold = sorted(set(gold[key]) - set(gen[key]))
        only_gen = sorted(set(gen[key]) - set(gold[key]))
        if only_gold or only_gen:
            delta[key] = {"only_gold": only_gold, "only_generated": only_gen}
    for key in ("limit", "distinct", "has_union"):
        if gold[key] != gen[key]:
            delta[key] = {"gold": gold[key], "generated": gen[key]}
    return delta


def _tagged(fp: Dict[str, Any]) -> Set[str]:
    items: Set[str] = set()
    items.update(f"table:{t}" for t in fp["tables"])
    items.update(f"join:{j}" for j in fp["joins"])
    items.update(f"agg:{a}" for a in fp["aggregates"])
    items.update(f"group:{g}" for g in fp["group_by"])
    items.update(f"pred:{p}" for p in fp["predicates"])
    if fp["distinct"]:
        items.add("distinct")
    if fp["has_union"]:
        items.add("union")
    if fp["limit"] is not None:
        items.add(f"limit:{fp['limit']}")
    return items


def overlap(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    left, right = _tagged(a), _tagged(b)
    union = left | right
    return round(len(left & right) / len(union), 3) if union else 1.0


# ── DAX (Power BI connections) ────────────────────────────────────────────────

_DAX_FUNC = re.compile(r"\b([A-Z][A-Z0-9_.]{2,})\s*\(")
_DAX_REF = re.compile(r"'[^']+'\[[^\]]+\]|\b[A-Za-z_][A-Za-z0-9_]*\[[^\]]+\]|\[[^\]]+\]")


def dax_fingerprint(dax: str) -> Dict[str, Any]:
    text = strip_comments(dax)
    functions = sorted({m.upper() for m in _DAX_FUNC.findall(text)})
    refs = sorted({re.sub(r"\s+", " ", r).lower() for r in _DAX_REF.findall(text)})
    return {"functions": functions, "refs": refs, "has_order": "ORDER BY" in text.upper()}


def dax_structural_equal(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return a["functions"] == b["functions"] and a["refs"] == b["refs"]


def dax_overlap(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    left = {f"fn:{f}" for f in a["functions"]} | {f"ref:{r}" for r in a["refs"]}
    right = {f"fn:{f}" for f in b["functions"]} | {f"ref:{r}" for r in b["refs"]}
    union = left | right
    return round(len(left & right) / len(union), 3) if union else 1.0


# ── Execution comparison (opt-in) ─────────────────────────────────────────────


def _norm_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
        return round(number, 4)
    if isinstance(value, str):
        text = value.strip()
        try:
            return round(float(text.replace(",", "").replace("$", "")), 4)
        except ValueError:
            return text.lower()
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value)


def _row_key(row: Iterable[Any]) -> Tuple[Any, ...]:
    return tuple(_norm_value(v) for v in row)


async def _run_gold(dsn: str, sql: str, max_rows: int) -> Tuple[List[str], List[Tuple[Any, ...]]]:
    import asyncpg  # noqa: PLC0415

    conn = await asyncpg.connect(dsn, timeout=20)
    try:
        await conn.execute("SET statement_timeout = 30000")
        async with conn.transaction(readonly=True):
            rows = await conn.fetch(sql)
        columns = list(rows[0].keys()) if rows else []
        return columns, [tuple(r.values()) for r in rows[:max_rows]]
    finally:
        await conn.close()


def compare_execution(payload: Dict[str, Any]) -> Dict[str, Any]:
    dsn = payload.get("gold_dsn")
    app_rows = payload.get("app_rows")
    app_columns = payload.get("app_columns") or []
    if not dsn or app_rows is None:
        return {"match": None, "detail": "not requested"}
    if (payload.get("dialect") or "postgres") != "postgres":
        return {"match": None, "detail": f"execution comparison supports postgres only (got {payload.get('dialect')})"}
    try:
        gold_columns, gold_rows = asyncio.run(_run_gold(dsn, payload["gold"], max_rows=20_000))
    except Exception as exc:  # noqa: BLE001
        return {"match": None, "detail": f"gold execution failed: {exc}"}

    app_tuples = [tuple(row.get(c) for c in app_columns) if isinstance(row, dict) else tuple(row) for row in app_rows]
    if gold_columns and app_columns and len(gold_columns) != len(app_columns):
        return {
            "match": False,
            "detail": f"column count differs: gold {len(gold_columns)} vs app {len(app_columns)}",
            "gold_rows": len(gold_rows), "app_rows": len(app_tuples),
        }
    gold_keys = sorted((_row_key(r) for r in gold_rows), key=repr)
    app_keys = sorted((_row_key(r) for r in app_tuples), key=repr)
    if payload.get("app_truncated") and len(app_keys) < len(gold_keys):
        # A capped result cannot prove the query right (a subset of the right rows
        # can come from a wrong query), so the comparison is inconclusive, not a pass.
        return {
            "match": None,
            "detail": f"inconclusive: the app result was truncated to {len(app_keys)} of {len(gold_keys)} gold rows",
            "gold_rows": len(gold_keys), "app_rows": len(app_keys),
        }
    match = gold_keys == app_keys
    detail = "identical multisets" if match else f"row sets differ (gold {len(gold_keys)}, app {len(app_keys)})"
    if not match and len(gold_keys) == len(app_keys):
        first = next((i for i, (g, a) in enumerate(zip(gold_keys, app_keys)) if g != a), None)
        if first is not None:
            detail += f"; first difference at sorted row {first}: gold={gold_keys[first]!r} app={app_keys[first]!r}"
    return {"match": match, "detail": detail, "gold_rows": len(gold_keys), "app_rows": len(app_keys)}


# ── Orchestration ─────────────────────────────────────────────────────────────


def grade(payload: Dict[str, Any]) -> Dict[str, Any]:
    gold = str(payload.get("gold") or "")
    generated = str(payload.get("generated") or "")
    dialect = payload.get("dialect") or None
    language = str(payload.get("language") or "sql").lower()

    out: Dict[str, Any] = {
        "tier": "mismatch",
        "verdicts": {"exact": False, "equivalent": False, "structural": False, "execution_match": None},
        "overlap": 0.0,
        "normalized": {"gold": normalize_text(gold), "generated": normalize_text(generated)},
        "canonical": {"gold": None, "generated": None},
        "fingerprint": {"gold": None, "generated": None},
        "delta": {},
        "diff": [],
        "execution": {"match": None, "detail": "not requested"},
        "error": None,
    }
    if not gold or not generated:
        out["error"] = "gold or generated statement is empty"
        return out

    out["verdicts"]["exact"] = out["normalized"]["gold"] == out["normalized"]["generated"]

    if language == "dax":
        fp_gold, fp_gen = dax_fingerprint(gold), dax_fingerprint(generated)
        out["fingerprint"] = {"gold": fp_gold, "generated": fp_gen}
        out["verdicts"]["structural"] = dax_structural_equal(fp_gold, fp_gen)
        out["overlap"] = dax_overlap(fp_gold, fp_gen)
    else:
        errors: List[str] = []
        parsed = True
        try:
            out["canonical"] = {"gold": canonical_sql(gold, dialect), "generated": canonical_sql(generated, dialect)}
            out["verdicts"]["equivalent"] = out["canonical"]["gold"] == out["canonical"]["generated"]
        except Exception as exc:  # noqa: BLE001
            errors.append(f"canonical: {exc}")
            parsed = False
        try:
            fp_gold, fp_gen = fingerprint(gold, dialect), fingerprint(generated, dialect)
            out["fingerprint"] = {"gold": fp_gold, "generated": fp_gen}
            out["verdicts"]["structural"] = structural_equal(fp_gold, fp_gen)
            out["overlap"] = overlap(fp_gold, fp_gen)
            out["delta"] = fingerprint_delta(fp_gold, fp_gen)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"fingerprint: {exc}")
            parsed = False
        if errors:
            out["error"] = "; ".join(errors)
        if not parsed and not out["verdicts"]["exact"]:
            out["tier"] = "ungraded"
            out["execution"] = compare_execution(payload)
            out["verdicts"]["execution_match"] = out["execution"].get("match")
            if out["verdicts"]["execution_match"]:
                out["tier"] = "execution_match"
            return out
        if not (out["verdicts"]["exact"] or out["verdicts"]["equivalent"]):
            try:
                from sqlglot.diff import Keep, diff as sqlglot_diff  # noqa: PLC0415

                edits = sqlglot_diff(_normalized_tree(gold, dialect), _normalized_tree(generated, dialect))
                out["diff"] = [f"{type(e).__name__}: {_edit_text(e)}" for e in edits if not isinstance(e, Keep)][:24]
            except Exception:  # noqa: BLE001 — diagnostics only
                pass

    out["execution"] = compare_execution(payload)
    out["verdicts"]["execution_match"] = out["execution"].get("match")

    for tier in ("exact", "equivalent", "structural", "execution_match"):
        if out["verdicts"].get(tier):
            out["tier"] = tier
            break
    return out


def _edit_text(edit: Any) -> str:
    for attr in ("expression", "source", "target"):
        node = getattr(edit, attr, None)
        if node is not None:
            try:
                return re.sub(r"\s+", " ", node.sql())[:120]
            except Exception:  # noqa: BLE001
                return str(node)[:120]
    return ""


def main() -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    try:
        result = grade(payload)
    except Exception as exc:  # noqa: BLE001 — the wrapper reports, the test decides
        result = {"tier": "error", "error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
