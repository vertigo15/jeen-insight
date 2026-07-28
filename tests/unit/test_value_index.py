"""Unit tests for engine-agnostic value (entity) linking."""

from __future__ import annotations

from src.metadata.value_index import (
    DEFAULT_TOKEN_THRESHOLD,
    ValueDomain,
    ValueDomainCache,
    covers_all_tokens,
    exact_value,
    is_refinement_set,
    match_values,
    normalize,
    score_value,
    search_tokens,
    token_matches,
    tokenize,
)

PRODUCTS = [
    "Mountain-300 Black, 38",
    "Mountain-300 Black, 40",
    "Mountain-300 Black, 44",
    "Mountain-500 Silver, 40",
    "Mountain-200 Black, 42",
    "Road-250 Red, 44",
    "Sport-100 Helmet, Red",
    "Water Bottle - 30 oz.",
]


class TestNormalisation:
    def test_punctuation_and_case_are_flattened(self):
        assert normalize("Mountain-300 Black, 38") == "mountain 300 black 38"

    def test_tokenize_splits_on_punctuation(self):
        assert tokenize("Mountain-300 Black, 38") == ["mountain", "300", "black", "38"]

    def test_empty_input_is_safe(self):
        assert normalize(None) == ""
        assert tokenize("   ") == []


class TestExactMatch:
    def test_differing_case_and_punctuation_still_match(self):
        assert exact_value("mountain 300 black 38", PRODUCTS) == "Mountain-300 Black, 38"

    def test_no_match_returns_none(self):
        assert exact_value("Speedster 900", PRODUCTS) is None


class TestMatching:
    def test_typo_matches_the_right_model(self):
        matches = match_values("Mountaiin 300", PRODUCTS)
        covered = [m.value for m in matches if m.covers_needle]
        assert covered == [
            "Mountain-300 Black, 38",
            "Mountain-300 Black, 40",
            "Mountain-300 Black, 44",
        ]

    def test_a_different_model_is_not_covered(self):
        matches = {m.value: m for m in match_values("Mountaiin 300", PRODUCTS)}
        assert matches["Mountain-500 Silver, 40"].covers_needle is False

    def test_unknown_value_matches_nothing(self):
        assert match_values("Speedster 900", PRODUCTS) == []

    def test_results_are_sorted_by_coverage_then_score(self):
        matches = match_values("Mountaiin 300", PRODUCTS)
        coverage_flags = [m.covers_needle for m in matches]
        assert coverage_flags == sorted(coverage_flags, reverse=True)

    def test_limit_is_respected(self):
        assert len(match_values("Mountain", PRODUCTS, limit=2, threshold=50)) == 2

    def test_empty_needle_or_domain(self):
        assert match_values("", PRODUCTS) == []
        assert match_values("Mountain", []) == []


class TestCoverage:
    def test_token_typo_still_counts_as_covered(self):
        assert covers_all_tokens(["mountaiin", "300"], ["mountain", "300", "black"])

    def test_missing_token_breaks_coverage(self):
        assert not covers_all_tokens(["mountain", "300"], ["mountain", "500", "black"])

    def test_score_is_symmetric_enough_for_substrings(self):
        assert score_value("road 250", "road 250 red 44") >= 90


class TestTokenMatching:
    def test_a_word_tolerates_a_typo(self):
        assert token_matches("mountaiin", "mountain")

    def test_a_number_does_not(self):
        """"300" scores 86 against "3000" — above any threshold that would still
        forgive a misspelled word — but they are different products."""
        assert score_value("300", "3000") > DEFAULT_TOKEN_THRESHOLD
        assert not token_matches("300", "3000")

    def test_an_identical_number_matches(self):
        assert token_matches("300", "300")

    def test_a_code_containing_digits_must_be_exact(self):
        assert not token_matches("m300", "m3000")

    def test_a_number_never_matches_a_word(self):
        assert not token_matches("300", "black")


class TestRefinementSet:
    def test_multi_token_needle_with_shared_qualifiers_is_a_refinement(self):
        covered = [m for m in match_values("Mountaiin 300", PRODUCTS) if m.covers_needle]
        assert is_refinement_set("Mountaiin 300", covered) is True

    def test_single_token_needle_is_never_a_refinement(self):
        """"Bike" covers "Bikes", "Bike Racks" and "Bike Stands" — summing all
        three silently would be wrong, so it must fall through to a question."""
        domain = ["Bikes", "Bike Racks", "Bike Stands"]
        covered = [m for m in match_values("Bike", domain, threshold=50) if m.covers_needle]
        assert len(covered) > 1
        assert is_refinement_set("Bike", covered) is False

    def test_single_match_is_not_a_refinement_set(self):
        covered = [m for m in match_values("Road 250", PRODUCTS) if m.covers_needle]
        assert len(covered) == 1
        assert is_refinement_set("Road 250", covered) is False


class TestSearchTokens:
    def test_long_tokens_are_truncated_so_late_typos_survive(self):
        assert search_tokens("Mountaiin 300") == ["moun", "300"]

    def test_short_tokens_are_kept_whole(self):
        assert search_tokens("Red 44") == ["red", "44"]

    def test_token_count_is_capped(self):
        assert len(search_tokens("one two three four five six")) == 4


class TestValueDomainCache:
    def test_round_trip(self):
        cache = ValueDomainCache(max_entries=4, ttl_seconds=60)
        key = ValueDomainCache.key("src", "u1", "Product", "Product Name")
        cache.put(key, ValueDomain(values=("a", "b"), complete=True))
        assert cache.get(key).values == ("a", "b")

    def test_users_do_not_share_entries(self):
        """Domains are read with the user's token, so RLS can make them differ."""
        cache = ValueDomainCache()
        key_a = ValueDomainCache.key("src", "u1", "Product", "Name")
        key_b = ValueDomainCache.key("src", "u2", "Product", "Name")
        assert key_a != key_b
        cache.put(key_a, ValueDomain(values=("secret",), complete=True))
        assert cache.get(key_b) is None

    def test_expired_entry_is_dropped(self):
        cache = ValueDomainCache(ttl_seconds=0)
        key = ValueDomainCache.key("src", "u1", "T", "C")
        cache.put(key, ValueDomain(values=("a",), complete=True))
        assert cache.get(key) is None

    def test_lru_eviction(self):
        cache = ValueDomainCache(max_entries=2, ttl_seconds=60)
        keys = [ValueDomainCache.key("src", "u", "T", f"C{i}") for i in range(3)]
        for k in keys:
            cache.put(k, ValueDomain(values=("a",), complete=True))
        assert cache.get(keys[0]) is None
        assert cache.get(keys[2]) is not None

    def test_datasets_do_not_share_entries(self):
        """A connection can be retargeted at another dataset within its TTL."""
        key_a = ValueDomainCache.key("src", "u1", "Product", "Name", "ds1")
        key_b = ValueDomainCache.key("src", "u1", "Product", "Name", "ds2")
        assert key_a != key_b

    def test_incomplete_key_is_rejected(self):
        assert ValueDomainCache.key("src", "u", "", "C") == ""
        assert ValueDomainCache().get("") is None

    def test_a_blank_user_is_never_cached(self):
        """Caching under a blank identity would let unrelated readers share one
        RLS-filtered domain."""
        assert ValueDomainCache.key("src", "", "T", "C") == ""
        assert ValueDomainCache.key("src", None, "T", "C") == ""
