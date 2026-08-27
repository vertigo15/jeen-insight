"""Tests for deterministic place resolution used by OSM maps."""

from __future__ import annotations

import asyncio
from collections import OrderedDict

from src.api import map_geocoding
from src.api.map_geocoding import (
    PlaceQuery,
    infer_geo_roles,
    resolve_osm_locations,
    resolve_place_queries,
    search_osm_places,
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


def _feature_collection(lat, lng):
    return {"type": "FeatureCollection", "features": [{
        "geometry": {"coordinates": [lng, lat]},
    }]}


def _enable_maptiler(monkeypatch, max_places=150):
    monkeypatch.setattr(map_geocoding.settings, "OSM_MAPS_ENABLED", True)
    monkeypatch.setattr(map_geocoding.settings, "OSM_TILE_URL", "https://tiles/{z}/{x}/{y}.png")
    monkeypatch.setattr(map_geocoding.settings, "OSM_TILE_API_KEY", "test-key")
    monkeypatch.setattr(map_geocoding.settings, "OSM_GEOCODER_API_KEY", "test-key")
    monkeypatch.setattr(map_geocoding.settings, "OSM_GEOCODER_PROVIDER", "maptiler")
    monkeypatch.setattr(map_geocoding.settings, "OSM_GEOCODER_BASE_URL", "")
    monkeypatch.setattr(map_geocoding.settings, "OSM_GEOCODER_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(map_geocoding.settings, "OSM_GEOCODER_MAX_UNIQUE_PLACES", max_places)
    monkeypatch.setattr(map_geocoding, "_cache", OrderedDict())
    monkeypatch.setattr(map_geocoding, "_last_request_at", 0.0)


def test_maptiler_batch_encodes_each_label_and_keeps_semicolon_delimiter(monkeypatch):
    _enable_maptiler(monkeypatch)
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return [_feature_collection(34.05, -118.24), _feature_collection(40.71, -74.0)]

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, url, params):
            calls.append((url, params))
            return Response()

    monkeypatch.setattr(map_geocoding.httpx, "AsyncClient", lambda **_: Client())
    resolved = asyncio.run(
        map_geocoding._maptiler_batch_resolution(["Los Angeles", "Town;Name"])
    )

    assert calls == [(
        "https://api.maptiler.com/geocoding/Los%20Angeles;Town%3BName.json",
        {"key": "test-key", "limit": 1},
    )]
    assert [item["status"] for item in resolved] == ["resolved", "resolved"]


def test_maptiler_batch_normalizes_single_feature_collection(monkeypatch):
    _enable_maptiler(monkeypatch)

    async def request(_):
        return 200, _feature_collection(34.05, -118.24)

    monkeypatch.setattr(map_geocoding, "_maptiler_batch_request", request)
    resolved = asyncio.run(map_geocoding._maptiler_batch_resolution(["Los Angeles"]))

    assert resolved == [{
        "status": "resolved", "lat": 34.05, "lng": -118.24, "source": "provider",
    }]


def test_maptiler_batch_rejects_mismatched_response_without_reordering(monkeypatch):
    _enable_maptiler(monkeypatch)

    async def request(_):
        return 200, [_feature_collection(34.05, -118.24)]

    monkeypatch.setattr(map_geocoding, "_maptiler_batch_request", request)
    resolved = asyncio.run(
        map_geocoding._maptiler_batch_resolution(["Los Angeles", "New York"])
    )

    assert resolved == [
        {"status": "unresolved", "source": "provider_error"},
        {"status": "unresolved", "source": "provider_error"},
    ]


def test_maptiler_batch_splits_oversized_request_without_losing_results(monkeypatch):
    _enable_maptiler(monkeypatch)
    calls = []

    async def request(labels):
        calls.append(labels)
        if len(labels) > 1:
            return 400, {}
        return 200, _feature_collection(34.05 if labels[0] == "Los Angeles" else 40.71, -118.24)

    monkeypatch.setattr(map_geocoding, "_maptiler_batch_request", request)
    resolved = asyncio.run(
        map_geocoding._maptiler_batch_resolution(["Los Angeles", "New York"])
    )

    assert calls == [["Los Angeles", "New York"], ["Los Angeles"], ["New York"]]
    assert [item["status"] for item in resolved] == ["resolved", "resolved"]


def test_resolve_place_queries_batches_80_uncached_maptiler_places(monkeypatch):
    _enable_maptiler(monkeypatch)
    batches = []

    async def batch(labels):
        batches.append(labels)
        return [{
            "status": "resolved", "lat": 10 + index, "lng": 20 + index, "source": "provider",
        } for index, _ in enumerate(labels)]

    monkeypatch.setattr(map_geocoding, "_maptiler_batch_resolution", batch)
    queries = [PlaceQuery(key=f"place-{index}", label=f"Place {index}") for index in range(80)]
    resolved = asyncio.run(resolve_place_queries(queries))

    assert [len(batch) for batch in batches] == [50, 30]
    assert len(resolved) == 80
    assert all(item["status"] == "resolved" for item in resolved.values())


def test_resolve_place_queries_skips_cached_and_local_values_before_batch(monkeypatch):
    _enable_maptiler(monkeypatch)
    map_geocoding._cache_set("cached", {"status": "resolved", "lat": 1.0, "lng": 2.0, "source": "provider"})
    batches = []

    async def batch(labels):
        batches.append(labels)
        return [{"status": "resolved", "lat": 3.0, "lng": 4.0, "source": "provider"}]

    monkeypatch.setattr(map_geocoding, "_maptiler_batch_resolution", batch)
    resolved = asyncio.run(resolve_place_queries([
        PlaceQuery(key="cached", label="Cached"),
        PlaceQuery(key="tel aviv", label="Tel Aviv"),
        PlaceQuery(key="los angeles", label="Los Angeles"),
    ]))

    assert batches == [["Los Angeles"]]
    assert resolved["cached"]["lat"] == 1.0
    assert resolved["tel aviv"]["source"] == "local"
    assert resolved["los angeles"]["source"] == "provider"


def test_place_search_reuses_cached_results(monkeypatch):
    _enable_maptiler(monkeypatch)
    calls = {"count": 0}

    async def search(query):
        calls["count"] += 1
        return [{"label": query, "lat": 48.8566, "lng": 2.3522}]

    monkeypatch.setattr(map_geocoding, "_maptiler_place_search", search)
    first = asyncio.run(search_osm_places("Paris"))
    second = asyncio.run(search_osm_places("paris"))

    assert first == second == [{"label": "Paris", "lat": 48.8566, "lng": 2.3522}]
    assert calls["count"] == 1


def test_provider_errors_expire_faster_than_resolved_places(monkeypatch):
    _enable_maptiler(monkeypatch)
    monkeypatch.setattr(map_geocoding.settings, "OSM_GEOCODER_CACHE_TTL_SECONDS", 2592000)
    monkeypatch.setattr(map_geocoding.settings, "OSM_GEOCODER_ERROR_CACHE_TTL_SECONDS", 300)

    assert map_geocoding._cache_ttl_seconds(
        {"status": "resolved", "lat": 1.0, "lng": 2.0, "source": "provider"}
    ) == 2592000
    assert map_geocoding._cache_ttl_seconds(
        {"status": "unresolved", "source": "provider_error"}
    ) == 300
    assert map_geocoding._cache_ttl_seconds({"status": "search", "results": []}) == 300
