"""Unit tests for the DAX lexer / read-only safety gate (src/connectors/dax_safety.py).

There is no sqlglot equivalent for DAX, so this pragmatic scope-aware lexer is the
only structural gate before the Power BI engine sees the query. These tests lock
down: read-only shape, balanced delimiters, single-EVALUATE, banned MDX/DMV/DDL
tokens (with names/strings blanked so false positives can't happen), identifier
extraction, and the TOPN row-cap wrapper.
"""

from __future__ import annotations

from src.connectors.dax_safety import (
    apply_topn_cap,
    banned_token,
    is_read_only_dax,
    lex_dax,
)


class TestReadOnlyGate:
    def test_simple_evaluate_is_read_only(self):
        ok, reason = is_read_only_dax("EVALUATE Sales")
        assert ok is True
        assert reason == ""

    def test_define_measure_then_evaluate_is_read_only(self):
        dax = 'DEFINE MEASURE \'Sales\'[M] = SUM(\'Sales\'[Amount])\nEVALUATE ROW("v", [M])'
        ok, reason = is_read_only_dax(dax)
        assert ok is True, reason

    def test_empty_is_rejected(self):
        ok, reason = is_read_only_dax("   ")
        assert ok is False
        assert "Empty" in reason

    def test_must_start_with_evaluate_or_define(self):
        ok, reason = is_read_only_dax("SELECT * FROM Sales")
        assert ok is False
        assert "EVALUATE or DEFINE" in reason

    def test_more_than_one_evaluate_rejected(self):
        ok, reason = is_read_only_dax("EVALUATE Sales\nEVALUATE Products")
        assert ok is False
        assert "more than one evaluate" in reason.lower()

    def test_unbalanced_parens_rejected(self):
        ok, reason = is_read_only_dax("EVALUATE FILTER(Sales, [x] = 1")
        assert ok is False
        assert "Unclosed" in reason or "Unbalanced" in reason

    def test_banned_ddl_token_rejected(self):
        ok, reason = is_read_only_dax('EVALUATE ROW("v", 1) DROP TABLE Sales')
        assert ok is False
        assert "disallowed" in reason.lower()

    def test_measure_named_select_is_not_a_banned_token(self):
        # The bracketed identifier is blanked before the banned-token scan, so a
        # measure literally called [Select Total] must NOT trip the SELECT ban.
        ok, reason = is_read_only_dax('EVALUATE ROW("v", [Select Total])')
        assert ok is True, reason

    def test_mdx_system_phrase_rejected(self):
        ok, reason = is_read_only_dax("EVALUATE $SYSTEM.DISCOVER_SESSIONS")
        assert ok is False


class TestLexer:
    def test_qualified_identifier(self):
        lex = lex_dax("EVALUATE FILTER(Sales, 'Sales'[Amount] > 0)")
        quals = [i for i in lex.identifiers if i.qualified]
        assert any(i.table == "Sales" and i.name == "Amount" for i in quals)

    def test_unqualified_measure_identifier(self):
        lex = lex_dax('EVALUATE ROW("v", [Total Sales])')
        unq = [i for i in lex.identifiers if not i.qualified]
        assert unq and unq[0].name == "Total Sales"
        assert unq[0].table is None

    def test_string_content_does_not_leak_into_tokens(self):
        lex = lex_dax('EVALUATE ROW("note", "this has DROP and SELECT inside")')
        assert banned_token(lex) is None

    def test_bracket_escape_double_close(self):
        lex = lex_dax('EVALUATE ROW("v", [Weird]]Name])')
        # ']]' is an escaped ']' inside the identifier — one identifier, balanced.
        assert lex.balanced is True

    def test_evaluate_count_and_define_kinds(self):
        lex = lex_dax("DEFINE VAR x = 1 MEASURE 'S'[M] = 1 EVALUATE Sales")
        assert lex.evaluate_count == 1
        assert "VAR" in lex.define_kinds
        assert "MEASURE" in lex.define_kinds

    def test_define_column_kind_detected(self):
        lex = lex_dax("DEFINE COLUMN 'S'[C] = 1 EVALUATE Sales")
        assert "COLUMN" in lex.define_kinds


class TestTopnCap:
    def test_wraps_simple_evaluate(self):
        assert apply_topn_cap("EVALUATE Sales", 100) == "EVALUATE\nTOPN(100, Sales)"

    def test_does_not_wrap_when_define_present(self):
        dax = "DEFINE VAR x = 1 EVALUATE Sales"
        assert apply_topn_cap(dax, 100) == dax

    def test_does_not_wrap_when_order_by_present(self):
        dax = "EVALUATE Sales ORDER BY 'Sales'[Amount] DESC"
        assert apply_topn_cap(dax, 100) == dax

    def test_does_not_double_wrap_leading_topn(self):
        dax = "EVALUATE TOPN(10, Sales, 'Sales'[Amount])"
        assert apply_topn_cap(dax, 100) == dax

    def test_noop_on_zero_or_missing_cap(self):
        assert apply_topn_cap("EVALUATE Sales", 0) == "EVALUATE Sales"
        assert apply_topn_cap("", 100) == ""
