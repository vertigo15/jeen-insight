"""Tests for preserving binary tile cache headers through the Flask UI proxy."""

from __future__ import annotations

from src import ui_app


class _BinaryResponse:
    status_code = 200
    content = b"tile-bytes"
    headers = {
        "Content-Type": "image/png",
        "Cache-Control": "private, max-age=3600",
        "ETag": '"tile-tag"',
        "Last-Modified": "Mon, 24 Aug 2026 12:00:00 GMT",
        "Server-Timing": "maptile;dur=12",
    }

    def json(self):
        raise ValueError("not JSON")


def test_flask_tile_forwarding_preserves_binary_cache_headers(monkeypatch):
    monkeypatch.setattr(ui_app.requests, "request", lambda **_: _BinaryResponse())

    with ui_app.app.test_request_context("/api/map-tiles/seamarks/3/4/2"):
        response = ui_app._forward("/api/map-tiles/seamarks/3/4/2")

    assert response.status_code == 200
    assert response.data == b"tile-bytes"
    assert response.headers["Content-Type"] == "image/png"
    assert response.headers["Cache-Control"] == "private, max-age=3600"
    assert response.headers["ETag"] == '"tile-tag"'
    assert response.headers["Last-Modified"] == "Mon, 24 Aug 2026 12:00:00 GMT"
    assert response.headers["Server-Timing"] == "maptile;dur=12"

