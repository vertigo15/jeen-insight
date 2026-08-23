"""Tests for deterministic place resolution used by OSM maps."""

from __future__ import annotations

import asyncio

from src.api import map_geocoding
from src.api.map_geocoding import PlaceQuery, resolve_place_queries, valid_coordinates


def test_valid_coordinates_enforces_wgs84_ranges():
    assert valid_coordinates(31.77, 35.21)
    assert not valid_coordinates(90.1, 0)
    assert not valid_coordinates(0, -180.1)
    assert not valid_coordinates("not a latitude", 35)


def test_local_city_lookup_resolves_without_provider(monkeypatch):
    monkeypatch.setattr(map_geocoding.settings, "OSM_MAPS_ENABLED", False)
    resolved = asyncio.run(
        resolve_place_queries([PlaceQuery(key="tel aviv", label="Tel Aviv")])
    )
    assert resolved["tel aviv"]["status"] == "resolved"
    assert resolved["tel aviv"]["source"] == "local"
    assert valid_coordinates(resolved["tel aviv"]["lat"], resolved["tel aviv"]["lng"])


def test_unconfigured_provider_never_attempts_network(monkeypatch):
    monkeypatch.setattr(map_geocoding.settings, "OSM_MAPS_ENABLED", True)
    monkeypatch.setattr(map_geocoding.settings, "OSM_TILE_URL", "https://tiles/{z}/{x}/{y}.png")
    monkeypatch.setattr(map_geocoding.settings, "OSM_GEOCODER_PROVIDER", "")
    resolved = asyncio.run(
        resolve_place_queries([PlaceQuery(key="unknown place", label="Unknown Place")])
    )
    assert resolved["unknown place"] == {"status": "unresolved", "source": "disabled"}
