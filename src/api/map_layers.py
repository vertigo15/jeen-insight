"""Server-approved raster map layers for OSM point visualizations.

Browser payloads contain only same-origin proxy URLs.  Provider templates and
credentials remain here so layer selection can never become an open proxy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.config import settings


_TILE_TOKENS = ("{z}", "{x}", "{y}")


@dataclass(frozen=True)
class MapTileLayer:
    """One configured raster basemap or transparent overlay."""

    id: str
    label: str
    kind: str
    tile_url: str
    attribution: str
    attribution_url: str
    source_template: str
    api_key: str = ""
    api_key_header: str = "Authorization"
    default_visible: bool = False

    def browser_config(self) -> dict[str, Any]:
        """Return only safe presentation data for the chart payload."""
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "tileUrl": self.tile_url,
            "attribution": self.attribution,
            "attributionUrl": self.attribution_url,
            "defaultVisible": self.default_visible,
        }


def valid_tile_template(template: str) -> bool:
    """Accept only XYZ templates suitable for the server-side tile proxy."""
    value = str(template or "").strip()
    return bool(value and all(token in value for token in _TILE_TOKENS))


def configured_map_layers() -> dict[str, MapTileLayer]:
    """Return the fixed allowlist of configured map layers by public ID."""
    layers = {
        "standard": MapTileLayer(
            id="standard",
            label="Standard",
            kind="basemap",
            tile_url="/api/map-tiles/{z}/{x}/{y}",
            attribution="© OpenStreetMap contributors",
            attribution_url="https://www.openstreetmap.org/copyright",
            source_template=settings.OSM_TILE_URL.strip(),
            api_key=settings.OSM_TILE_API_KEY,
            api_key_header=settings.OSM_TILE_API_KEY_HEADER,
            default_visible=True,
        )
    }
    seamarks_template = settings.OSM_SEAMARKS_TILE_URL.strip()
    if settings.OSM_SEAMARKS_ENABLED and valid_tile_template(seamarks_template):
        layers["seamarks"] = MapTileLayer(
            id="seamarks",
            label="Seamarks",
            kind="overlay",
            tile_url="/api/map-tiles/seamarks/{z}/{x}/{y}",
            attribution="© OpenSeaMap contributors",
            attribution_url="https://www.openseamap.org/",
            source_template=seamarks_template,
            api_key=settings.OSM_SEAMARKS_TILE_API_KEY,
            api_key_header=settings.OSM_SEAMARKS_TILE_API_KEY_HEADER,
        )
    maritime_template = settings.OSM_MARITIME_TILE_URL.strip()
    if valid_tile_template(maritime_template):
        layers["maritime"] = MapTileLayer(
            id="maritime",
            label="Ocean and maritime boundaries",
            kind="basemap",
            tile_url="/api/map-tiles/maritime/{z}/{x}/{y}",
            attribution="© MapTiler © OpenStreetMap contributors",
            attribution_url="https://www.maptiler.com/copyright/",
            source_template=maritime_template,
            api_key=settings.OSM_MARITIME_TILE_API_KEY or settings.OSM_TILE_API_KEY,
            api_key_header=(
                settings.OSM_MARITIME_TILE_API_KEY_HEADER
                or settings.OSM_TILE_API_KEY_HEADER
            ),
        )
    return layers


def browser_map_layers() -> dict[str, list[dict[str, Any]]]:
    """Return the safe layer manifest embedded in map chart options."""
    layers = configured_map_layers()
    return {
        "basemaps": [
            layer.browser_config() for layer in layers.values() if layer.kind == "basemap"
        ],
        "overlays": [
            layer.browser_config() for layer in layers.values() if layer.kind == "overlay"
        ],
        # Natural Earth is a bundled public-domain cartographic reference. It
        # avoids a paid tile service and must never be presented as legal or
        # navigational boundary data.
        "vectorOverlays": [{
            "id": "maritime-boundaries",
            "label": "Maritime boundaries (reference)",
            "kind": "vector",
            "dataUrl": (
                "/static/chart-feature/assets/maps/"
                "ne_50m_admin_0_boundary_lines_maritime_indicator.geojson"
            ),
            "attribution": "Natural Earth (public domain)",
            "attributionUrl": "https://www.naturalearthdata.com/",
            "defaultVisible": False,
        }],
    }

