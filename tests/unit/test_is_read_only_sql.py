"""Tests for the SELECT-only guard in `src.tools.sql_tool`."""

from __future__ import annotations

import pytest

from src.connectors.base import assert_read_only_query
from src.tools.sql_tool import is_read_only_sql


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT 1", True),
        ("  select 1", True),
        ("SELECT *\nFROM foo", True),
        ("WITH cte AS (SELECT 1) SELECT * FROM cte", True),
        ("with cte as (select 1) select * from cte", True),
        ("-- a leading comment\nSELECT 1", True),
        ("/* foo */ SELECT 1", True),
        ("-- one\n/* two */\n-- three\nSELECT 1", True),
        ("INSERT INTO foo VALUES (1)", False),
        ("UPDATE foo SET x = 1", False),
        ("DELETE FROM foo", False),
        ("DROP TABLE foo", False),
        ("TRUNCATE foo", False),
        ("GRANT ALL ON foo TO bar", False),
        ("CREATE TABLE foo (id INT)", False),
        # Comment-prefixed mutation: the comment is stripped, then the leading
        # keyword check fails on DELETE.
        ("/* trick */ DELETE FROM foo", False),
        # EXPLAIN is intentionally rejected today (could revisit; reject is
        # the safer default for an LLM-driven runner).
        ("EXPLAIN SELECT 1", False),
        ("", False),
        ("   ", False),
        ("\n\n", False),
    ],
)
def test_is_read_only_sql(sql, expected):
    assert is_read_only_sql(sql) is expected


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT * FROM users WHERE name = 'x'",
        "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
        "SELECT a, b FROM t1 JOIN t2 ON t1.id = t2.id",
        "SELECT 1 UNION SELECT 2",
        # A semicolon inside a string literal must not be treated as a
        # statement separator.
        "SELECT * FROM t WHERE note = 'a;b'",
        # A trailing semicolon is fine (single statement).
        "SELECT 1;",
    ],
)
def test_assert_read_only_query_allows_single_read_queries(sql):
    assert assert_read_only_query(sql, "postgres") is None


@pytest.mark.parametrize(
    "sql",
    [
        # Multiple statements — the leading-keyword gate alone would pass these.
        "SELECT 1; DELETE FROM users",
        "SELECT 1; SELECT 2",
        # DML hidden inside a CTE (leading keyword is WITH).
        "WITH changed AS (DELETE FROM users RETURNING id) SELECT * FROM changed",
        "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x",
        "WITH x AS (UPDATE t SET a = 1 RETURNING *) SELECT * FROM x",
    ],
)
def test_assert_read_only_query_blocks_unsafe_structures(sql):
    # Sanity: the leading-keyword gate does NOT catch these — the structural
    # check is what protects engines without a read-only transaction.
    assert is_read_only_sql(sql) is True
    assert assert_read_only_query(sql, "postgres") is not None
