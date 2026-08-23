"""Tests for deterministic place resolution used by OSM maps."""

from __future__ import annotations

import asyncio

from src.api import map_geocoding
from src.api.map_geocoding import (
    PlaceQuery,
    infer_geo_roles,
    resolve_osm_locations,
    resolve_place_queries,
    valid_coordinates,
)


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


def test_geo_role_scan_prefers_dimgeography_place_parts_not_keys_or_postal_measure():
    roles = infer_geo_roles(
        {
            "columns": [
                "GeographyKey",
                "City",
                "StateProvinceName",
                "EnglishCountryRegionName",
                "PostalCode",
            ],
            "rows": [[1, "Paris", "Texas", "United States", "75460"]],
        }
    )

    assert roles["candidates"]["place"] == ["City"]
    assert roles["candidates"]["admin1"] == ["StateProvinceName"]
    assert roles["candidates"]["country"] == ["EnglishCountryRegionName"]
    assert roles["candidates"]["postal"] == ["PostalCode"]
    assert not roles["coordinate_pairs"]


def test_compound_place_query_uses_city_state_country(monkeypatch):
    seen = []

    async def resolve(queries):
        seen.extend(queries)
        return {query.key: {"status": "unresolved", "source": "disabled"} for query in queries}

    monkeypatch.setattr(map_geocoding, "resolve_place_queries", resolve)
    resolved = asyncio.run(
        resolve_osm_locations(
            {
                "location": "City",
                "location_parts": {
                    "place": "City",
                    "admin1": "StateProvinceName",
                    "country": "EnglishCountryRegionName",
                    "postal": "PostalCode",
                },
            },
            {
                "columns": ["City", "StateProvinceName", "EnglishCountryRegionName", "PostalCode"],
                "rows": [["Paris", "Texas", "United States", "75460"]],
            },
        )
    )

    assert seen[0].label == "Paris, Texas, United States, 75460"
    assert seen[0].key == "paris|texas|united states|75460"
    assert resolved[seen[0].key]["status"] == "unresolved"


def test_maptiler_search_results_use_geojson_lng_lat_and_deduplicate():
    results = map_geocoding._maptiler_search_results(
        {
            "features": [
                {
                    "place_name": "Los Angeles, California, United States",
                    "geometry": {"coordinates": [-118.2437, 34.0522]},
                },
                {
                    "text": "Duplicate Los Angeles",
                    "geometry": {"coordinates": [-118.2437, 34.0522]},
                },
                {
                    "text": "Invalid",
                    "geometry": {"coordinates": [300, 120]},
                },
            ]
        }
    )

    assert results == [{
        "label": "Los Angeles, California, United States",
        "lat": 34.0522,
        "lng": -118.2437,
    }]
