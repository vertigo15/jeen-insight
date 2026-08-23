"""Deterministic, provider-configured geocoding for OSM point charts.

The chart builder remains pure: this module resolves place names before the
builder receives a spec, then persists the resolved coordinates in the chart
payload.  It deliberately does not use an LLM or an MCP call.
"""

from __future__ import annotations

import asyncio
import math
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

from src.api.map_locations import lookup_israel_city
from src.config import settings


@dataclass(frozen=True)
class PlaceQuery:
    """A normalized place name and the key used in persisted chart specs."""

    key: str
    label: str


_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_request_lock = asyncio.Lock()
_last_request_at = 0.0


def location_key(value: Any) -> str:
    """Return a stable, case-insensitive key without exposing it in logs."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return " ".join(text.split())


def valid_coordinates(latitude: Any, longitude: Any) -> bool:
    """True only for finite WGS84 latitude/longitude pairs."""
    try:
        lat = float(latitude)
        lng = float(longitude)
    except (TypeError, ValueError):
        return False
    return math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180


def osm_maps_enabled() -> bool:
    """Map rendering is dark until an operator explicitly enables a tile source."""
    return bool(settings.OSM_MAPS_ENABLED and settings.OSM_TILE_URL.strip())


def geocoding_enabled() -> bool:
    """Only an explicitly configured provider may receive place names."""
    return bool(
        osm_maps_enabled()
        and settings.OSM_GEOCODER_PROVIDER.strip().lower() == "nominatim"
        and settings.OSM_GEOCODER_BASE_URL.strip()
    )


def chart_capabilities() -> dict[str, Any]:
    """Return public capability flags without exposing provider credentials."""
    return {
        "osm_map": {
            "enabled": osm_maps_enabled(),
            "geocoding_enabled": geocoding_enabled(),
        }
    }


def _cache_get(key: str) -> dict[str, Any] | None:
    cached = _cache.get(key)
    if not cached:
        return None
    expires_at, value = cached
    if expires_at <= time.monotonic():
        _cache.pop(key, None)
        return None
    return dict(value)


def _cache_set(key: str, value: dict[str, Any]) -> None:
    ttl = max(0, int(settings.OSM_GEOCODER_CACHE_TTL_SECONDS))
    if ttl:
        _cache[key] = (time.monotonic() + ttl, dict(value))


def _local_resolution(label: str) -> dict[str, Any] | None:
    city = lookup_israel_city(label)
    if not city or not valid_coordinates(city.get("lat"), city.get("lng")):
        return None
    return {
        "status": "resolved",
        "lat": float(city["lat"]),
        "lng": float(city["lng"]),
        "source": "local",
    }


async def _nominatim_resolution(label: str) -> dict[str, Any]:
    """Resolve one location through a Nominatim-compatible configured endpoint."""
    global _last_request_at

    timeout = max(0.1, float(settings.OSM_GEOCODER_TIMEOUT_SECONDS))
    min_interval = max(0.0, float(settings.OSM_GEOCODER_MIN_INTERVAL_SECONDS))
    headers = {"User-Agent": settings.OSM_GEOCODER_USER_AGENT.strip() or "Jeen Insights"}
    if settings.OSM_GEOCODER_API_KEY:
        headers[settings.OSM_GEOCODER_API_KEY_HEADER.strip() or "Authorization"] = (
            settings.OSM_GEOCODER_API_KEY
        )

    async with _request_lock:
        remaining = min_interval - (time.monotonic() - _last_request_at)
        if remaining > 0:
            await asyncio.sleep(remaining)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.get(
                    settings.OSM_GEOCODER_BASE_URL,
                    params={"q": label, "format": "jsonv2", "limit": 5, "addressdetails": 1},
                    headers=headers,
                )
            _last_request_at = time.monotonic()
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            # Do not log the raw location: it can be personal data.
            return {"status": "unresolved", "source": "provider_error"}

    if not isinstance(payload, list):
        return {"status": "unresolved", "source": "provider_error"}

    coordinates: set[tuple[float, float]] = set()
    for item in payload:
        if not isinstance(item, dict) or not valid_coordinates(item.get("lat"), item.get("lon")):
            continue
        coordinates.add((round(float(item["lat"]), 7), round(float(item["lon"]), 7)))

    if len(coordinates) == 1:
        lat, lng = coordinates.pop()
        return {"status": "resolved", "lat": lat, "lng": lng, "source": "provider"}
    if len(coordinates) > 1:
        return {"status": "ambiguous", "source": "provider"}
    return {"status": "unresolved", "source": "provider"}


async def resolve_place_queries(queries: Iterable[PlaceQuery]) -> dict[str, dict[str, Any]]:
    """Resolve unique place names with local lookup first, then configured HTTP."""
    unique: dict[str, PlaceQuery] = {query.key: query for query in queries if query.key and query.label}
    max_places = max(0, int(settings.OSM_GEOCODER_MAX_UNIQUE_PLACES))
    result: dict[str, dict[str, Any]] = {}

    for index, (key, query) in enumerate(unique.items()):
        if max_places and index >= max_places:
            result[key] = {"status": "unresolved", "source": "limit"}
            continue
        cached = _cache_get(key)
        if cached:
            result[key] = cached
            continue
        local = _local_resolution(query.label)
        if local:
            _cache_set(key, local)
            result[key] = local
            continue
        if not geocoding_enabled():
            result[key] = {"status": "unresolved", "source": "disabled"}
            continue
        resolved = await _nominatim_resolution(query.label)
        _cache_set(key, resolved)
        result[key] = resolved
    return result


def _read_cell(row: Any, column: str | None, index: int) -> Any:
    if isinstance(row, dict):
        return row.get(column) if column else None
    if isinstance(row, (list, tuple)) and 0 <= index < len(row):
        return row[index]
    return None


async def resolve_osm_locations(spec: dict[str, Any], dataset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Find rows needing a place lookup and resolve each unique place once."""
    columns = dataset.get("columns") or []
    rows = dataset.get("rows") or []
    location_name = spec.get("location") or spec.get("x")
    latitude_name = spec.get("latitude")
    longitude_name = spec.get("longitude")
    location_index = columns.index(location_name) if location_name in columns else -1
    latitude_index = columns.index(latitude_name) if latitude_name in columns else -1
    longitude_index = columns.index(longitude_name) if longitude_name in columns else -1
    queries: list[PlaceQuery] = []

    for row in rows:
        lat = _read_cell(row, latitude_name, latitude_index)
        lng = _read_cell(row, longitude_name, longitude_index)
        if valid_coordinates(lat, lng):
            continue
        label = str(_read_cell(row, location_name, location_index) or "").strip()
        key = location_key(label)
        if key:
            queries.append(PlaceQuery(key=key, label=label))

    return await resolve_place_queries(queries)
