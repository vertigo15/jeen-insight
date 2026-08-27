"""In-process LRU + TTL cache of proxied raster map tiles.

Tiles are identical for every user. Caching them here avoids a MapTiler hop on
pan, zoom, and repeat views. Eviction, restarts, and extra replicas only affect
speed — the proxy still fetches on a miss.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from src.config import settings


@dataclass(frozen=True)
class CachedTile:
    """One cached raster tile and the validators the browser may reuse."""

    content: bytes
    content_type: str
    etag: str
    last_modified: str


class MapTileCache:
    """Thread-safe LRU + TTL store of tile bytes, keyed by layer and XYZ."""

    def __init__(self):
        self._store: OrderedDict[str, tuple[float, CachedTile]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _key(layer_id: str, z: int, x: int, y: int) -> str:
        return f"{layer_id}/{z}/{x}/{y}"

    @staticmethod
    def _limits() -> tuple[int, int]:
        return (
            max(0, int(settings.OSM_TILE_CACHE_MAX_ENTRIES)),
            max(0, int(settings.OSM_TILE_CACHE_TTL_SECONDS)),
        )

    def get(self, layer_id: str, z: int, x: int, y: int) -> CachedTile | None:
        max_entries, ttl = self._limits()
        if not ttl or not max_entries:
            return None
        key = self._key(layer_id, z, x, y)
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            inserted, tile = entry
            if now - inserted > ttl:
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            return tile

    def put(
        self,
        layer_id: str,
        z: int,
        x: int,
        y: int,
        *,
        content: bytes,
        content_type: str,
        etag: str = "",
        last_modified: str = "",
    ) -> None:
        max_entries, ttl = self._limits()
        if not ttl or not max_entries or not content:
            return
        key = self._key(layer_id, z, x, y)
        tile = CachedTile(
            content=content,
            content_type=content_type or "image/png",
            etag=etag,
            last_modified=last_modified,
        )
        with self._lock:
            self._store[key] = (time.monotonic(), tile)
            self._store.move_to_end(key)
            while len(self._store) > max_entries:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


map_tile_cache = MapTileCache()
