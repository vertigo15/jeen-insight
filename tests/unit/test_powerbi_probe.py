"""Unit tests for the bounded read-only DAX value probes."""

from __future__ import annotations

from src.connectors.dax_safety import apply_topn_cap, is_read_only_dax, lex_dax
from src.connectors.powerbi_probe import (
    MAX_PROBE_ROWS,
    PowerBiValueProbe,
    build_contains_dax,
    build_distinct_values_dax,
    column_ref,
    escape_column_name,
    escape_dax_string,
    escape_table_name,
)


class _Client:
    """Captures the DAX it is handed and replays a canned response."""

    def __init__(self, rows=None, error=None):
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls = []

    async def execute_dax(self, dax, token, *, max_rows=10000):
        self.calls.append((dax, token, max_rows))
        if self.error:
            return {"error": self.error, "error_type": "bad_request"}
        return {"columns": ["Product Name"], "rows": self.rows, "row_count": len(self.rows)}


def _probe(client, token="tok"):
    async def get_token():
        return token

    return PowerBiValueProbe(client, get_token)


def _rows(*values):
    return [{"Product Name": v} for v in values]


class TestEscaping:
    def test_string_literal_doubles_quotes(self):
        assert escape_dax_string('say "hi"') == 'say ""hi""'

    def test_table_name_doubles_apostrophes(self):
        assert escape_table_name("Bob's Table") == "Bob''s Table"

    def test_column_name_doubles_closing_bracket(self):
        assert escape_column_name("Weird]Col") == "Weird]]Col"

    def test_column_ref_is_fully_qualified(self):
        assert column_ref("Product", "Product Name") == "'Product'[Product Name]"


class TestGeneratedDax:
    def test_distinct_query_passes_the_read_only_gate(self):
        dax = build_distinct_values_dax("Product", "Product Name", 1001)
        assert is_read_only_dax(dax) == (True, "")

    def test_contains_query_passes_the_read_only_gate(self):
        dax = build_contains_dax("Product", "Model Name", ["moun", "300"], 40)
        assert is_read_only_dax(dax) == (True, "")

    def test_probe_is_already_capped_so_topn_is_not_re_applied(self):
        dax = build_distinct_values_dax("Product", "Product Name", 50)
        assert apply_topn_cap(dax, 10) == dax

    def test_awkward_identifiers_survive_a_lexer_round_trip(self):
        dax = build_distinct_values_dax("Bob's Table", "Weird]Col", 10)
        assert is_read_only_dax(dax) == (True, "")
        identifiers = lex_dax(dax).identifiers
        assert identifiers[0].table == "Bob's Table"
        assert identifiers[0].name == "Weird]Col"

    def test_contains_ors_every_fragment(self):
        dax = build_contains_dax("Product", "Name", ["a", "b", "c"], 10)
        assert dax.count("CONTAINSSTRING") == 3
        assert dax.count("||") == 2


class TestDistinctValues:
    async def test_complete_domain(self):
        client = _Client(_rows("A", "B"))
        result = await _probe(client).distinct_values("Product", "Name", limit=10)
        assert result.values == ("A", "B")
        assert (result.complete, result.ok) == (True, True)

    async def test_oversized_domain_is_flagged_incomplete(self):
        """Fetching limit+1 doubles as the cardinality check."""
        client = _Client(_rows("A", "B", "C"))
        result = await _probe(client).distinct_values("Product", "Name", limit=2)
        assert result.values == ("A", "B")
        assert (result.complete, result.ok) == (False, True)

    async def test_fetches_one_more_than_the_limit(self):
        client = _Client(_rows("A"))
        await _probe(client).distinct_values("Product", "Name", limit=100)
        assert "TOPN(101," in client.calls[0][0]

    async def test_limit_is_hard_capped(self):
        """The marker row lives inside the ceiling rather than one past it."""
        client = _Client(_rows("A"))
        await _probe(client).distinct_values("Product", "Name", limit=10_000_000)
        assert f"TOPN({MAX_PROBE_ROWS}," in client.calls[0][0]

    async def test_error_is_not_reported_as_an_empty_column(self):
        """A failed probe must be distinguishable from a genuinely empty one:
        callers treat "empty and complete" as proof a value does not exist."""
        client = _Client(error="Power BI said no")
        result = await _probe(client).distinct_values("Product", "Name", limit=10)
        assert result.ok is False
        assert result.values == ()

    async def test_genuinely_empty_column_is_a_complete_read(self):
        result = await _probe(_Client([])).distinct_values("Product", "Name", limit=10)
        assert (result.values, result.complete, result.ok) == ((), True, True)

    async def test_blank_and_null_cells_are_dropped(self):
        client = _Client([{"Name": "A"}, {"Name": None}, {"Name": "  "}, {"Name": "B"}])
        result = await _probe(client).distinct_values("Product", "Name", limit=10)
        assert result.values == ("A", "B")

    async def test_dropped_blanks_cannot_disguise_a_truncated_read(self):
        """Truncation is judged on raw rows: limit+1 rows means more exist even
        if one of them was blank and got filtered out."""
        client = _Client([{"Name": "A"}, {"Name": "B"}, {"Name": "  "}])
        result = await _probe(client).distinct_values("Product", "Name", limit=2)
        assert result.complete is False

    async def test_missing_token_is_a_failure_not_an_empty_column(self):
        client = _Client(_rows("A"))
        result = await _probe(client, token="").distinct_values("Product", "Name", limit=10)
        assert result.ok is False
        assert client.calls == []


class TestAuthorize:
    async def test_no_token_means_not_entitled(self):
        assert await _probe(_Client(), token="").authorize() is None

    async def test_identity_becomes_the_cache_scope(self):
        """Keying by grant rather than app user keeps a relinked Power BI
        account from inheriting the previous identity's RLS view."""

        async def identity():
            return "grant-7|ann@contoso.com"

        probe = PowerBiValueProbe(_Client(), lambda: None, identity)
        assert await probe.authorize() == "grant-7|ann@contoso.com"


class TestContainsValues:
    async def test_returns_matching_values(self):
        client = _Client(_rows("Mountain-300"))
        result = await _probe(client).contains_values(
            "Product", "Model Name", ["moun"], limit=10
        )
        assert result.values == ("Mountain-300",)
        assert (result.complete, result.ok) == (True, True)

    async def test_truncated_search_is_flagged_incomplete(self):
        client = _Client(_rows("A", "B", "C"))
        result = await _probe(client).contains_values("Product", "Name", ["a"], limit=2)
        assert result.complete is False

    async def test_no_fragments_skips_the_call(self):
        client = _Client(_rows("X"))
        result = await _probe(client).contains_values("Product", "Name", [], limit=10)
        assert result.values == ()
        assert client.calls == []

    async def test_blank_fragments_are_ignored(self):
        client = _Client(_rows("X"))
        await _probe(client).contains_values("Product", "Name", ["", "ab"], limit=10)
        assert client.calls[0][0].count("CONTAINSSTRING") == 1

    async def test_error_is_not_reported_as_no_matches(self):
        client = _Client(error="boom")
        result = await _probe(client).contains_values("Product", "Name", ["ab"], limit=10)
        assert result.ok is False
