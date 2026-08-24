"""Deterministic, provider-configured geocoding for OSM point charts.

The chart builder remains pure: this module resolves place names before the
builder receives a spec, then persists the resolved coordinates in the chart
payload.  It deliberately does not use an LLM or an MCP call.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote

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
_MAPTILER_BATCH_SIZE = 50

_LATITUDE_NAME_RE = re.compile(r"(^|_)(lat|latitude)$", re.I)
_LONGITUDE_NAME_RE = re.compile(r"(^|_)(lon|lng|long|longitude)$", re.I)
_PLACE_NAME_RE = re.compile(r"(city|town|municipality|locality|place)", re.I)
_ADMIN1_NAME_RE = re.compile(r"(state|province|district|region)", re.I)
_COUNTRY_NAME_RE = re.compile(r"(country|nation)", re.I)
_POSTAL_NAME_RE = re.compile(r"(postal|zip)", re.I)
_UNSAFE_LOCATION_NAME_RE = re.compile(r"(\bip\b|ip_?address|address|locator)", re.I)
_IDENTIFIER_NAME_RE = re.compile(
    r"(id|key|code|number|no|num|year|month|day|quarter|qtr|week|"
    r"rank|index|idx|seq)s?$",
    re.I,
)


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


def _row_cell(row: Any, column: str, index: int) -> Any:
    if isinstance(row, dict):
        return row.get(column)
    if isinstance(row, (list, tuple)) and 0 <= index < len(row):
        return row[index]
    return None


def _metadata_role_hints(metadata_hints: dict[str, str] | None) -> dict[str, list[str]]:
    hints = metadata_hints or {}
    aliases = {"location": "place", "latitude": "latitude", "longitude": "longitude"}
    result: dict[str, list[str]] = {}
    for source_role, column in hints.items():
        role = aliases.get(source_role)
        if role and isinstance(column, str) and column:
            result.setdefault(role, []).append(column)
    return result


def infer_geo_roles(
    dataset: dict[str, Any], metadata_hints: dict[str, str] | None = None
) -> dict[str, Any]:
    """Return safe, ranked geographic-column candidates for a result dataset.

    This only examines result column names and values already available to the
    chart request. It deliberately excludes address/IP/identifier-like fields
    from location and measure suggestions.
    """
    columns = [str(column) for column in (dataset.get("columns") or [])]
    rows = list(dataset.get("rows") or [])
    candidates: dict[str, list[str]] = {
        "latitude": [], "longitude": [], "place": [], "admin1": [], "country": [], "postal": [],
    }
    metadata = _metadata_role_hints(metadata_hints)

    for column in columns:
        name = column.strip()
        if not name:
            continue
        unsafe = bool(_UNSAFE_LOCATION_NAME_RE.search(name))
        identifier = bool(_IDENTIFIER_NAME_RE.search(name))
        if name in metadata.get("latitude", []) or (
            _LATITUDE_NAME_RE.search(name) and not identifier
        ):
            candidates["latitude"].append(name)
        if name in metadata.get("longitude", []) or (
            _LONGITUDE_NAME_RE.search(name) and not identifier
        ):
            candidates["longitude"].append(name)
        if unsafe:
            continue
        if name in metadata.get("place", []) or (_PLACE_NAME_RE.search(name) and not identifier):
            candidates["place"].append(name)
        if (
            _ADMIN1_NAME_RE.search(name)
            and not _COUNTRY_NAME_RE.search(name)
            and not identifier
        ):
            candidates["admin1"].append(name)
        if _COUNTRY_NAME_RE.search(name):
            candidates["country"].append(name)
        if _POSTAL_NAME_RE.search(name):
            candidates["postal"].append(name)

    valid_pairs: list[dict[str, Any]] = []
    for latitude in candidates["latitude"]:
        lat_index = columns.index(latitude)
        for longitude in candidates["longitude"]:
            lng_index = columns.index(longitude)
            matched = 0
            non_null = 0
            for row in rows:
                lat = _row_cell(row, latitude, lat_index)
                lng = _row_cell(row, longitude, lng_index)
                if lat is not None or lng is not None:
                    non_null += 1
                if valid_coordinates(lat, lng) and not (float(lat) == 0 and float(lng) == 0):
                    matched += 1
            coverage = matched / max(1, len(rows))
            if coverage:
                valid_pairs.append({
                    "latitude": latitude,
                    "longitude": longitude,
                    "coverage": round(coverage, 4),
                    "matched": matched,
                    "non_null": non_null,
                })
    valid_pairs.sort(key=lambda pair: (-pair["coverage"], pair["latitude"], pair["longitude"]))
    return {"candidates": candidates, "coordinate_pairs": valid_pairs}


def osm_maps_enabled() -> bool:
    """Map rendering is dark until an operator explicitly enables a tile source."""
    return bool(settings.OSM_MAPS_ENABLED and settings.OSM_TILE_URL.strip())


def geocoding_enabled() -> bool:
    """Only an explicitly configured provider may receive place names."""
    return bool(
        osm_maps_enabled()
        and settings.OSM_GEOCODER_PROVIDER.strip().lower() in {"nominatim", "maptiler"}
        and (
            settings.OSM_GEOCODER_BASE_URL.strip()
            or settings.OSM_GEOCODER_PROVIDER.strip().lower() == "maptiler"
        )
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
    parts = [part.strip() for part in label.split(",") if part.strip()]
    country = parts[-1].casefold() if len(parts) > 1 else ""
    if country and country not in {"israel", "ישראל"}:
        return None
    city = lookup_israel_city(parts[0] if parts else label)
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


def _maptiler_feature_collection_resolution(payload: Any) -> dict[str, Any]:
    """Convert one MapTiler FeatureCollection into the chart resolution schema."""
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        return {"status": "unresolved", "source": "provider_error"}
    coordinates: set[tuple[float, float]] = set()
    for feature in features:
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        coordinate = geometry.get("coordinates") if isinstance(geometry, dict) else None
        if (
            not isinstance(coordinate, list)
            or len(coordinate) < 2
            or not valid_coordinates(coordinate[1], coordinate[0])
        ):
            continue
        coordinates.add((round(float(coordinate[1]), 7), round(float(coordinate[0]), 7)))
    if len(coordinates) == 1:
        lat, lng = coordinates.pop()
        return {"status": "resolved", "lat": lat, "lng": lng, "source": "provider"}
    if len(coordinates) > 1:
        return {"status": "ambiguous", "source": "provider"}
    return {"status": "unresolved", "source": "provider"}


async def _maptiler_batch_request(labels: list[str]) -> tuple[int, Any] | None:
    """Fetch one MapTiler batch after rate limiting, without logging its URL."""
    global _last_request_at

    if not labels:
        return 200, []
    timeout = max(0.1, float(settings.OSM_GEOCODER_TIMEOUT_SECONDS))
    min_interval = max(0.0, float(settings.OSM_GEOCODER_MIN_INTERVAL_SECONDS))
    base_url = settings.OSM_GEOCODER_BASE_URL.strip() or "https://api.maptiler.com/geocoding"
    api_key = settings.OSM_GEOCODER_API_KEY or settings.OSM_TILE_API_KEY
    if not api_key:
        return None
    # MapTiler requires a literal semicolon between independently encoded
    # queries; encoding the entire path would corrupt the batch delimiter.
    encoded_labels = ";".join(quote(label, safe="") for label in labels)

    async with _request_lock:
        remaining = min_interval - (time.monotonic() - _last_request_at)
        if remaining > 0:
            await asyncio.sleep(remaining)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.get(
                    f"{base_url.rstrip('/')}/{encoded_labels}.json",
                    # A compound city/state/country label is already
                    # disambiguated. Keep only the top provider match so nearby
                    # administrative features cannot create false ambiguity.
                    params={"key": api_key, "limit": 1},
                )
            _last_request_at = time.monotonic()
        except (httpx.HTTPError, ValueError):
            return None
    try:
        return response.status_code, response.json()
    except ValueError:
        return None


async def _maptiler_batch_resolution(labels: list[str]) -> list[dict[str, Any]]:
    """Resolve up to 50 compound places through MapTiler in one request."""
    if not labels:
        return []
    if len(labels) > _MAPTILER_BATCH_SIZE:
        result: list[dict[str, Any]] = []
        for start in range(0, len(labels), _MAPTILER_BATCH_SIZE):
            result.extend(await _maptiler_batch_resolution(
                labels[start:start + _MAPTILER_BATCH_SIZE]
            ))
        return result

    response = await _maptiler_batch_request(labels)
    if response is None:
        status = "disabled" if not (
            settings.OSM_GEOCODER_API_KEY or settings.OSM_TILE_API_KEY
        ) else "provider_error"
        return [{"status": "unresolved", "source": status}] * len(labels)

    status_code, payload = response
    if status_code == 400 and len(labels) > 1:
        # MapTiler rejects an oversized path. Split only that bad batch so a
        # single long city label cannot make every other location unavailable.
        midpoint = len(labels) // 2
        return (
            await _maptiler_batch_resolution(labels[:midpoint])
            + await _maptiler_batch_resolution(labels[midpoint:])
        )
    if status_code < 200 or status_code >= 300:
        return [{"status": "unresolved", "source": "provider_error"}] * len(labels)

    # MapTiler returns a FeatureCollection for one query and an ordered array
    # of FeatureCollections for batches. Keep keys aligned with input labels.
    collections = [payload] if len(labels) == 1 and isinstance(payload, dict) else payload
    if not isinstance(collections, list) or len(collections) != len(labels):
        return [{"status": "unresolved", "source": "provider_error"}] * len(labels)
    return [_maptiler_feature_collection_resolution(item) for item in collections]


async def _maptiler_resolution(label: str) -> dict[str, Any]:
    """Backward-compatible single-place wrapper around batch geocoding."""
    return (await _maptiler_batch_resolution([label]))[0]


def _maptiler_search_results(payload: Any) -> list[dict[str, Any]]:
    """Normalize safe browser-facing search results from MapTiler GeoJSON."""
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        return []
    results: list[dict[str, Any]] = []
    seen: set[tuple[float, float]] = set()
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry")
        coordinate = geometry.get("coordinates") if isinstance(geometry, dict) else None
        if (
            not isinstance(coordinate, list)
            or len(coordinate) < 2
            or not valid_coordinates(coordinate[1], coordinate[0])
        ):
            continue
        lat, lng = round(float(coordinate[1]), 7), round(float(coordinate[0]), 7)
        if (lat, lng) in seen:
            continue
        seen.add((lat, lng))
        label = str(feature.get("place_name") or feature.get("text") or "").strip()
        if not label:
            continue
        results.append({"label": label, "lat": lat, "lng": lng})
    return results


async def _maptiler_place_search(query: str) -> list[dict[str, Any]]:
    """Search MapTiler's managed geocoder without exposing its credential."""
    global _last_request_at

    timeout = max(0.1, float(settings.OSM_GEOCODER_TIMEOUT_SECONDS))
    min_interval = max(0.0, float(settings.OSM_GEOCODER_MIN_INTERVAL_SECONDS))
    base_url = settings.OSM_GEOCODER_BASE_URL.strip() or "https://api.maptiler.com/geocoding"
    api_key = settings.OSM_GEOCODER_API_KEY or settings.OSM_TILE_API_KEY
    if not api_key:
        return []
    async with _request_lock:
        remaining = min_interval - (time.monotonic() - _last_request_at)
        if remaining > 0:
            await asyncio.sleep(remaining)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.get(
                    f"{base_url.rstrip('/')}/{quote(query, safe='')}.json",
                    params={"key": api_key, "limit": 5},
                )
            _last_request_at = time.monotonic()
            response.raise_for_status()
            return _maptiler_search_results(response.json())
        except (httpx.HTTPError, ValueError):
            return []


async def _nominatim_place_search(query: str) -> list[dict[str, Any]]:
    """Search a configured Nominatim-compatible service for map navigation."""
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
                    params={"q": query, "format": "jsonv2", "limit": 5, "addressdetails": 1},
                    headers=headers,
                )
            _last_request_at = time.monotonic()
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return []
    if not isinstance(payload, list):
        return []
    results = []
    for item in payload:
        if not isinstance(item, dict) or not valid_coordinates(item.get("lat"), item.get("lon")):
            continue
        label = str(item.get("display_name") or "").strip()
        if label:
            results.append({
                "label": label,
                "lat": round(float(item["lat"]), 7),
                "lng": round(float(item["lon"]), 7),
            })
    return results


async def search_osm_places(query: str) -> list[dict[str, Any]]:
    """Return up to five provider-ranked navigation results for a user query."""
    normalized = " ".join(str(query or "").split())[:200]
    if len(normalized) < 2 or not geocoding_enabled():
        return []
    provider = settings.OSM_GEOCODER_PROVIDER.strip().lower()
    if provider == "maptiler":
        return await _maptiler_place_search(normalized)
    return await _nominatim_place_search(normalized)


async def resolve_place_queries(queries: Iterable[PlaceQuery]) -> dict[str, dict[str, Any]]:
    """Resolve unique place names with local lookup first, then configured HTTP."""
    unique: dict[str, PlaceQuery] = {query.key: query for query in queries if query.key and query.label}
    max_places = max(0, int(settings.OSM_GEOCODER_MAX_UNIQUE_PLACES))
    result: dict[str, dict[str, Any]] = {}
    provider = settings.OSM_GEOCODER_PROVIDER.strip().lower()
    pending_maptiler: list[tuple[str, PlaceQuery]] = []

    for index, (key, query) in enumerate(unique.items()):
        if max_places and index >= max_places:
            result[key] = {"status": "limited", "source": "limit"}
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
        if provider == "maptiler":
            pending_maptiler.append((key, query))
            continue
        resolved = await _nominatim_resolution(query.label)
        _cache_set(key, resolved)
        result[key] = resolved

    for start in range(0, len(pending_maptiler), _MAPTILER_BATCH_SIZE):
        chunk = pending_maptiler[start:start + _MAPTILER_BATCH_SIZE]
        resolutions = await _maptiler_batch_resolution([query.label for _, query in chunk])
        for (key, _), resolved in zip(chunk, resolutions):
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
    location_parts = spec.get("location_parts")
    location_parts = location_parts if isinstance(location_parts, dict) else {}
    location_name = location_parts.get("place") or spec.get("location") or spec.get("x")
    latitude_name = spec.get("latitude")
    longitude_name = spec.get("longitude")
    location_index = columns.index(location_name) if location_name in columns else -1
    latitude_index = columns.index(latitude_name) if latitude_name in columns else -1
    longitude_index = columns.index(longitude_name) if longitude_name in columns else -1
    context_columns = [
        location_parts.get(role)
        for role in ("place", "admin1", "country", "postal")
        if location_parts.get(role) in columns
    ]
    context_indexes = {column: columns.index(column) for column in context_columns}
    queries: list[PlaceQuery] = []

    for row in rows:
        lat = _read_cell(row, latitude_name, latitude_index)
        lng = _read_cell(row, longitude_name, longitude_index)
        if valid_coordinates(lat, lng) and not (float(lat) == 0 and float(lng) == 0):
            continue
        parts = [
            str(_read_cell(row, column, context_indexes[column]) or "").strip()
            for column in context_columns
        ]
        if not parts and location_index >= 0:
            parts = [str(_read_cell(row, location_name, location_index) or "").strip()]
        parts = [part for part in parts if part]
        label = ", ".join(parts)
        key = location_key("|".join(parts))
        if key:
            queries.append(PlaceQuery(key=key, label=label))

    return await resolve_place_queries(queries)
