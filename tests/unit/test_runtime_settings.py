"""Unit tests for the live-editable runtime settings tier.

These cover the typed coercion added so the text-to-DAX entity-resolution
controls (a bool kill switch, an int ceiling, a float threshold) can live
alongside the original int-only guardrails.
"""

from __future__ import annotations

import pytest

from src.metadata import runtime_settings as rs


class TestClamp:
    def test_ints_are_constrained_to_bounds(self):
        assert rs.clamp("max_result_rows", 10**9) == 1_000_000
        assert rs.clamp("max_result_rows", 0) == 1
        assert rs.clamp("conversation_context_turns", 7) == 7

    def test_int_keys_accept_the_strings_the_db_returns(self):
        assert rs.clamp("db_statement_timeout_ms", "5000") == 5000

    def test_floats_keep_their_precision(self):
        assert rs.clamp("dax_entity_match_threshold", "82.5") == 82.5

    def test_floats_are_constrained_to_bounds(self):
        assert rs.clamp("dax_entity_match_threshold", 250.0) == 100.0
        assert rs.clamp("dax_entity_match_threshold", -3) == 0.0

    @pytest.mark.parametrize("raw", ["true", "TRUE", " yes ", "on", "1", "t"])
    def test_truthy_spellings(self, raw):
        assert rs.clamp("dax_entity_resolution_enabled", raw) is True

    @pytest.mark.parametrize("raw", ["false", "no", "off", "0", "", "banana"])
    def test_everything_else_is_false(self, raw):
        assert rs.clamp("dax_entity_resolution_enabled", raw) is False

    def test_native_bools_pass_through(self):
        assert rs.clamp("dax_entity_cross_column_enabled", True) is True
        assert rs.clamp("dax_entity_cross_column_enabled", False) is False

    def test_unparseable_numbers_raise(self):
        with pytest.raises(ValueError):
            rs.clamp("max_result_rows", "not a number")


class TestBounds:
    def test_booleans_are_omitted(self):
        """The UI reads this map only to constrain numeric inputs."""
        b = rs.bounds()
        assert "dax_entity_resolution_enabled" not in b
        assert "dax_entity_cross_column_enabled" not in b

    def test_numeric_keys_are_present(self):
        b = rs.bounds()
        assert b["max_result_rows"] == {"min": 1, "max": 1_000_000}
        assert b["dax_entity_match_threshold"] == {"min": 0.0, "max": 100.0}


class TestDefaults:
    def test_every_declared_key_has_a_default(self):
        """A key in _SPECS with no dataclass field would KeyError at read time."""
        defaults = rs._defaults()
        for key in rs._SPECS:
            assert hasattr(defaults, key), f"{key} has no default"

    def test_entity_resolution_defaults_on(self):
        """It shipped enabled; the DB tier must not silently flip it."""
        assert rs._defaults().dax_entity_resolution_enabled is True


class TestReadFallback:
    async def test_a_dead_database_yields_env_defaults(self, monkeypatch):
        """Unlike app_flags, this tier must not fail closed: it governs a live
        query path, so a DB blip must not change query behaviour."""
        rs.invalidate_cache()

        async def boom():
            raise RuntimeError("no pool")

        monkeypatch.setattr("src.metadata.get_metadata_pool", boom, raising=False)
        got = await rs.get_runtime_settings(use_cache=False)
        assert got == rs._defaults()
        rs.invalidate_cache()


class TestSetter:
    async def test_unknown_key_is_rejected(self):
        with pytest.raises(KeyError):
            await rs.set_runtime_setting("not_a_setting", 1)

    async def test_bad_value_is_rejected_before_touching_the_db(self):
        with pytest.raises(ValueError):
            await rs.set_runtime_setting("max_result_rows", "banana")
