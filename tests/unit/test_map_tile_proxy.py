"""Regression tests for secure, cacheable OSM raster tile proxying."""

from __future__ import annotations

from src.api.map_tile_cache import map_tile_cache
from src.api.routes import charts


class _TileResponse:
    status_code = 200
    content = b"tile-bytes"
    headers = {
        "content-type": "image/png",
        "ETag": '"upstream-tag"',
        "Last-Modified": "Mon, 24 Aug 2026 12:00:00 GMT",
    }

    def raise_for_status(self):
        return None


class _TileClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, *_args, **_kwargs):
        return _TileResponse()


def _enable_standard_tiles(monkeypatch):
    map_tile_cache.clear()
    monkeypatch.setattr(charts.settings, "OSM_MAPS_ENABLED", True)
    monkeypatch.setattr(
        charts.settings,
        "OSM_TILE_URL",
        "https://tiles.example.test/{z}/{x}/{y}.png?key={api_key}",
    )
    monkeypatch.setattr(charts.settings, "OSM_TILE_API_KEY", "private-key")
    monkeypatch.setattr(charts.settings, "OSM_TILE_CACHE_TTL_SECONDS", 604800)
    monkeypatch.setattr(charts.settings, "OSM_TILE_CACHE_MAX_ENTRIES", 2048)


def test_standard_tile_proxy_preserves_private_cache_validators(client, monkeypatch):
    _enable_standard_tiles(monkeypatch)
    monkeypatch.setattr(charts.httpx, "AsyncClient", lambda **_: _TileClient())

    response = client.get("/api/map-tiles/3/4/2")

    assert response.status_code == 200
    assert response.content == b"tile-bytes"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"].startswith("private, max-age=604800")
    assert response.headers["etag"] == '"upstream-tag"'
    assert response.headers["last-modified"] == "Mon, 24 Aug 2026 12:00:00 GMT"
    assert response.headers["x-cache"] == "MISS"


def test_standard_tile_proxy_serves_repeat_requests_from_memory(client, monkeypatch):
    _enable_standard_tiles(monkeypatch)
    fetches = {"count": 0}

    class _CountingClient(_TileClient):
        async def get(self, *_args, **_kwargs):
            fetches["count"] += 1
            return _TileResponse()

    monkeypatch.setattr(charts.httpx, "AsyncClient", lambda **_: _CountingClient())

    first = client.get("/api/map-tiles/3/4/2")
    second = client.get("/api/map-tiles/3/4/2")
    not_modified = client.get("/api/map-tiles/3/4/2", headers={"If-None-Match": '"upstream-tag"'})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.content == b"tile-bytes"
    assert second.headers["x-cache"] == "HIT"
    assert not_modified.status_code == 304
    assert fetches["count"] == 1


def test_named_layer_requires_server_allowlist(client, monkeypatch):
    _enable_standard_tiles(monkeypatch)

    response = client.get("/api/map-tiles/not-configured/3/4/2")

    assert response.status_code == 404
    assert response.json()["detail"] == "Map layer is unavailable"


def test_named_seamarks_layer_uses_same_origin_proxy(client, monkeypatch):
    _enable_standard_tiles(monkeypatch)
    monkeypatch.setattr(charts.settings, "OSM_SEAMARKS_ENABLED", True)
    monkeypatch.setattr(
        charts.settings,
        "OSM_SEAMARKS_TILE_URL",
        "https://seamarks.example.test/{z}/{x}/{y}.png",
    )
    monkeypatch.setattr(charts.httpx, "AsyncClient", lambda **_: _TileClient())

    response = client.get("/api/map-tiles/seamarks/3/4/2")

    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("private, max-age=604800")

