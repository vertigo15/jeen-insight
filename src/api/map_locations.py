"""Local map location normalization and lookup data.

No network calls are made here. These helpers keep map matching deterministic
for air-gapped deployments and make unmatched locations explicit instead of
letting ECharts silently drop them.
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional


_CITY_DATA_PATH = (
    Path(__file__).resolve().parent.parent
    / "static"
    / "chart-feature"
    / "assets"
    / "data"
    / "israel_cities.json"
)
_MAPS_DIR = (
    Path(__file__).resolve().parent.parent
    / "static"
    / "chart-feature"
    / "assets"
    / "maps"
)
_MAP_ASSET_FILES = {
    "world": "world.json",
    "world_detailed": "world_detailed.json",
    "israel_districts": "israel_districts.json",
}


def _key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\u200e\u200f]", "", text)
    text = text.replace("&", " and ")
    text = re.sub(r"['`״׳]", "", text)
    text = re.sub(r"[^0-9a-z\u0590-\u05ff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_COUNTRY_ALIASES = {
    "united states": "United States",
    "united states of america": "United States",
    "usa": "United States",
    "u s a": "United States",
    "us": "United States",
    "u s": "United States",
    "uk": "United Kingdom",
    "u k": "United Kingdom",
    "great britain": "United Kingdom",
    "britain": "United Kingdom",
    "england": "United Kingdom",
    "uae": "United Arab Emirates",
    "u a e": "United Arab Emirates",
    "united arab emirates": "United Arab Emirates",
    "south korea": "Korea",
    "korea republic of": "Korea",
    "republic of korea": "Korea",
    "north korea": "Dem. Rep. Korea",
    "russia": "Russia",
    "russian federation": "Russia",
    "vietnam": "Vietnam",
    "viet nam": "Vietnam",
    "iran": "Iran",
    "islamic republic of iran": "Iran",
    "syria": "Syria",
    "czechia": "Czech Rep.",
    "czech republic": "Czech Rep.",
    "dominican republic": "Dominican Rep.",
    "bosnia and herzegovina": "Bosnia and Herz.",
    "central african republic": "Central African Rep.",
    "democratic republic of the congo": "Dem. Rep. Congo",
    "dr congo": "Dem. Rep. Congo",
    "congo kinshasa": "Dem. Rep. Congo",
    "republic of the congo": "Congo",
    "congo brazzaville": "Congo",
    "tanzania": "Tanzania",
    "laos": "Lao PDR",
    "moldova": "Moldova",
    "bolivia": "Bolivia",
    "venezuela": "Venezuela",
    "palestine": "Palestine",
    "state of palestine": "Palestine",
    "israel": "Israel",
    "ישראל": "Israel",
}


def _feature_name(feature: dict[str, Any]) -> Optional[str]:
    props = feature.get("properties") if isinstance(feature, dict) else None
    if not isinstance(props, dict):
        return None
    name = props.get("name") or props.get("NAME") or props.get("shapeName")
    return str(name).strip() if name else None


@lru_cache(maxsize=None)
def map_feature_names(map_name: str) -> frozenset[str]:
    """Return canonical feature names bundled for a local map asset."""
    filename = _MAP_ASSET_FILES.get(map_name)
    if not filename:
        return frozenset()
    try:
        geojson = json.loads((_MAPS_DIR / filename).read_text(encoding="utf-8"))
    except Exception:
        return frozenset()
    names = {
        name
        for feature in geojson.get("features", [])
        if (name := _feature_name(feature))
    }
    return frozenset(names)


def available_map_names() -> frozenset[str]:
    return frozenset(_MAP_ASSET_FILES)


_ISRAEL_DISTRICT_ALIASES = {
    "center": "Center District",
    "central": "Center District",
    "center district": "Center District",
    "central district": "Center District",
    "מחוז המרכז": "Center District",
    "מרכז": "Center District",
    "haifa": "Haifa District",
    "haifa district": "Haifa District",
    "מחוז חיפה": "Haifa District",
    "חיפה": "Haifa District",
    "jerusalem": "Jerusalem District",
    "jerusalem district": "Jerusalem District",
    "מחוז ירושלים": "Jerusalem District",
    "ירושלים": "Jerusalem District",
    "north": "North District",
    "northern": "North District",
    "north district": "North District",
    "northern district": "North District",
    "מחוז הצפון": "North District",
    "צפון": "North District",
    "south": "South District",
    "southern": "South District",
    "south district": "South District",
    "southern district": "South District",
    "מחוז הדרום": "South District",
    "דרום": "South District",
    "tel aviv": "Tel Aviv District",
    "tel aviv district": "Tel Aviv District",
    "tel aviv yafo": "Tel Aviv District",
    "מחוז תל אביב": "Tel Aviv District",
    "תל אביב": "Tel Aviv District",
}


@lru_cache(maxsize=1)
def _city_index() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(_CITY_DATA_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = []

    idx: dict[str, dict[str, Any]] = {}
    for city in raw:
        if not isinstance(city, dict):
            continue
        names = [city.get("name"), *(city.get("aliases") or [])]
        for name in names:
            k = _key(name)
            if k:
                idx[k] = city
    return idx


def canonical_region(map_name: str, value: Any) -> Optional[str]:
    """Return the canonical feature name for a region map, or None."""
    k = _key(value)
    if not k:
        return None
    if map_name == "israel_districts":
        candidate = _ISRAEL_DISTRICT_ALIASES.get(k)
    else:
        candidate = _COUNTRY_ALIASES.get(k) or str(value).strip()
    if not candidate:
        return None
    return candidate if candidate in map_feature_names(map_name) else None


def lookup_israel_city(value: Any) -> Optional[dict[str, Any]]:
    """Return local Israeli city metadata for a name/alias, or None."""
    city = _city_index().get(_key(value))
    if not city:
        return None
    return {
        "name": city.get("name"),
        "lat": city.get("lat"),
        "lng": city.get("lng"),
        "district": city.get("district"),
    }


def infer_map_name(value: Any) -> Optional[str]:
    """Infer a regional map from a location value when possible."""
    if _key(value) in _ISRAEL_DISTRICT_ALIASES:
        return "israel_districts"
    if lookup_israel_city(value):
        return "israel_districts"
    return None
