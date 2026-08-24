"""Tests for the server-approved OSM base and overlay layer manifest."""

import json
from pathlib import Path

from src.api import map_layers


def test_map_layer_manifest_hides_disabled_or_invalid_optional_layers(monkeypatch):
    monkeypatch.setattr(map_layers.settings, "OSM_SEAMARKS_ENABLED", False)
    monkeypatch.setattr(map_layers.settings, "OSM_MARITIME_TILE_URL", "")

    manifest = map_layers.browser_map_layers()

    assert [layer["id"] for layer in manifest["basemaps"]] == ["standard"]
    assert manifest["overlays"] == []
    assert manifest["basemaps"][0]["tileUrl"] == "/api/map-tiles/{z}/{x}/{y}"
    assert manifest["vectorOverlays"] == [{
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
    }]


def test_map_layer_manifest_exposes_only_safe_proxy_urls(monkeypatch):
    monkeypatch.setattr(map_layers.settings, "OSM_SEAMARKS_ENABLED", True)
    monkeypatch.setattr(
        map_layers.settings,
        "OSM_SEAMARKS_TILE_URL",
        "https://tiles.example.test/seamarks/{z}/{x}/{y}.png?key={api_key}",
    )
    monkeypatch.setattr(
        map_layers.settings,
        "OSM_MARITIME_TILE_URL",
        "https://tiles.example.test/maritime/{z}/{x}/{y}.png?key={api_key}",
    )
    monkeypatch.setattr(map_layers.settings, "OSM_MARITIME_TILE_API_KEY", "private-key")

    manifest = map_layers.browser_map_layers()
    sources = map_layers.configured_map_layers()

    assert [layer["id"] for layer in manifest["basemaps"]] == ["standard", "maritime"]
    assert manifest["overlays"][0]["id"] == "seamarks"
    assert manifest["overlays"][0]["tileUrl"] == "/api/map-tiles/seamarks/{z}/{x}/{y}"
    assert "example.test" not in str(manifest)
    assert "private-key" not in str(manifest)
    assert sources["maritime"].api_key == "private-key"


def test_tile_template_requires_xyz_placeholders():
    assert map_layers.valid_tile_template("https://tiles.example/{z}/{x}/{y}.png")
    assert not map_layers.valid_tile_template("https://tiles.example/{z}/{x}.png")


def test_bundled_maritime_reference_asset_is_geojson_lines():
    asset = (
        Path(__file__).resolve().parents[2]
        / "src/static/chart-feature/assets/maps"
        / "ne_50m_admin_0_boundary_lines_maritime_indicator.geojson"
    )
    payload = json.loads(asset.read_text(encoding="utf-8"))

    assert payload["type"] == "FeatureCollection"
    assert payload["features"]
    assert {feature["geometry"]["type"] for feature in payload["features"]} <= {
        "LineString", "MultiLineString",
    }

