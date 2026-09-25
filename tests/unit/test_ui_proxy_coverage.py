"""Every ``/api/...`` URL the browser code calls must have a Flask (BFF) route.

The browser never reaches FastAPI directly: the UI container proxies each
endpoint explicitly (``src/ui_app.py``) and has no catch-all. A FastAPI route
without its proxy works in unit tests (which call FastAPI) and in the e2e
harness (which replaces ``fetch``) and 404s in every real deployment — which
is exactly how two endpoints shipped broken. This test closes that gap.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI_APP = ROOT / "src" / "ui_app.py"
STATIC = ROOT / "src" / "static"

_ROUTE = re.compile(r"@app\.route\(\s*['\"]([^'\"]+)['\"]")
# Literal URLs only; templated ones (`/api/conversations/${id}/...`) are
# checked by the tests that exercise those features.
_JS_URL = re.compile(r"['\"`](/api/[A-Za-z0-9_\-/]+)['\"`]")


def _flask_patterns() -> list[re.Pattern]:
    patterns = []
    for route in _ROUTE.findall(UI_APP.read_text(encoding="utf-8")):
        escaped = re.escape(route)
        # <path:x> spans slashes; any other converter is one segment.
        escaped = re.sub(r"<path:[^>]+>", "PATHSEG", escaped)
        escaped = re.sub(r"<[^>]+>", "[^/]+", escaped)
        escaped = escaped.replace("PATHSEG", ".+")
        patterns.append(re.compile(f"^{escaped}/?$"))
    return patterns


def _flask_path_prefixes() -> list[str]:
    """Prefixes served by a ``<path:...>`` route: a JS constant holding that
    prefix (``const BASE = '/api/admin/analytics'`` used as ``${BASE}/x``) is a
    base URL, not a call, and is covered by that route."""
    prefixes = []
    for route in _ROUTE.findall(UI_APP.read_text(encoding="utf-8")):
        if "<path:" in route:
            prefixes.append(route.split("<path:", 1)[0].rstrip("/"))
    return prefixes


def _browser_api_urls() -> set[str]:
    urls: set[str] = set()
    for path in STATIC.rglob("*.js"):
        if "/vendor/" in str(path):
            continue
        for url in _JS_URL.findall(path.read_text(encoding="utf-8", errors="ignore")):
            urls.add(url.rstrip("/"))
    return urls


def test_every_browser_api_url_has_a_flask_proxy_route():
    patterns = _flask_patterns()
    prefixes = _flask_path_prefixes()
    urls = _browser_api_urls()
    assert len(urls) > 20, "the URL scan found suspiciously few endpoints"
    missing = sorted(
        u for u in urls
        if not any(p.match(u) for p in patterns) and not any(u == pre or u.startswith(pre + "/") for pre in prefixes)
    )
    assert missing == [], f"browser calls these endpoints but src/ui_app.py does not proxy them: {missing}"
