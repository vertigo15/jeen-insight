"""Guards that keep "add a language" safe for the interface catalogs.

- every shipped locale has a catalog and every catalog is a shipped locale;
- every catalog defines exactly the key set of the English source of truth;
- messages are text-only (no HTML), never empty, and keep the same ICU
  argument names as English;
- Hebrew plural messages cover ``one``/``two``/``other``;
- the keys rendered server-side (Jinja / Flask) stay within the plain
  ``{arg}`` subset that ``src.i18n.translate`` implements.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src import i18n
from src.i18n import LOCALES, flatten

ROOT = Path(__file__).resolve().parents[2]
MESSAGES = ROOT / "src" / "i18n" / "messages"

_ARG = re.compile(r"\{\s*([A-Za-z_][A-Za-z0-9_]*)")
_PLURAL = re.compile(r"\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*plural\s*,")
_SELECTOR = re.compile(r"\{\s*[A-Za-z_][A-Za-z0-9_]*\s*,\s*(plural|select|selectordinal)\b")
_HTML_TAG = re.compile(r"<[a-zA-Z/][^>]*>")

REQUIRED_PLURAL_CATEGORIES = {"en": {"other"}, "he": {"one", "two", "other"}}


def _load(locale: str) -> dict:
    return flatten(json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8")))


def _catalog_files() -> set[str]:
    return {p.stem for p in MESSAGES.glob("*.json")}


def _server_rendered_keys() -> set[str]:
    """Keys that Jinja templates or Flask code render through ``t()``."""
    keys: set[str] = set()
    pattern = re.compile(r"""\b_?t\(\s*['"]([a-zA-Z0-9_.]+)['"]""")
    for path in list((ROOT / "src" / "templates").glob("*.html")) + [
        ROOT / "src" / "ui_app.py",
        ROOT / "src" / "auth_db.py",
    ]:
        keys.update(pattern.findall(path.read_text(encoding="utf-8")))
    # friendly_db_error_key returns login.errors.* keys by value.
    keys.update(
        re.findall(r'return "(login\.errors\.[a-zA-Z]+)"', (ROOT / "src" / "auth_db.py").read_text())
    )
    return keys


def _plural_categories(message: str) -> list[set[str]]:
    """Category names of every ``{x, plural, ...}`` block (excluding ``=n``)."""
    found: list[set[str]] = []
    for match in _PLURAL.finditer(message):
        depth = 0
        cats: set[str] = set()
        i = match.end()
        token = ""
        while i < len(message):
            ch = message[i]
            if ch == "{":
                if depth == 0:
                    name = token.strip()
                    if name and not name.startswith("="):
                        cats.add(name)
                    token = ""
                depth += 1
            elif ch == "}":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0:
                token += ch
            i += 1
        found.append(cats)
    return found


def test_one_catalog_per_registered_locale():
    assert _catalog_files() == set(LOCALES)
    assert "en" in LOCALES


@pytest.mark.parametrize("locale", LOCALES)
def test_catalog_values_are_non_empty_text_without_html(locale):
    catalog = _load(locale)
    empty = [k for k, v in catalog.items() if not v.strip()]
    assert empty == [], f"{locale}: empty values"
    html = [k for k, v in catalog.items() if _HTML_TAG.search(v)]
    assert html == [], f"{locale}: catalogs are text-only"


@pytest.mark.parametrize("locale", LOCALES)
def test_braces_balance_in_every_message(locale):
    broken = [k for k, v in _load(locale).items() if v.count("{") != v.count("}")]
    assert broken == [], f"{locale}: unbalanced braces"


@pytest.mark.parametrize("locale", [loc for loc in LOCALES if loc != "en"])
def test_catalog_matches_english_key_set(locale):
    source, target = _load("en"), _load(locale)
    missing = sorted(set(source) - set(target))
    extra = sorted(set(target) - set(source))
    assert (missing, extra) == ([], []), f"{locale}: missing={missing[:10]} extra={extra[:10]}"


@pytest.mark.parametrize("locale", [loc for loc in LOCALES if loc != "en"])
def test_catalog_keeps_the_same_icu_arguments(locale):
    source, target = _load("en"), _load(locale)
    mismatched = [
        k for k, v in source.items()
        if k in target and set(_ARG.findall(v)) != set(_ARG.findall(target[k]))
    ]
    assert mismatched == []


@pytest.mark.parametrize("locale", LOCALES)
def test_plural_messages_cover_the_locale_categories(locale):
    required = REQUIRED_PLURAL_CATEGORIES.get(locale, {"other"})
    incomplete = []
    for key, message in _load(locale).items():
        for cats in _plural_categories(message):
            if not required <= cats:
                incomplete.append((key, sorted(required - cats)))
    assert incomplete == []


def test_server_rendered_keys_exist_and_stay_in_the_plain_subset():
    """Flask/Jinja format with ``src.i18n.translate`` (plain ``{arg}`` only)."""
    english = _load("en")
    keys = _server_rendered_keys()
    assert keys, "expected server-rendered keys to be discovered"
    unknown = sorted(k for k in keys if k not in english)
    assert unknown == []
    for locale in LOCALES:
        catalog = _load(locale)
        complex_keys = sorted(k for k in keys if _SELECTOR.search(catalog[k]))
        assert complex_keys == [], f"{locale}: server keys must not use plural/select"


_CLIENT_KEY_CALL = re.compile(
    r"""(?<![A-Za-z0-9_$.])(?:t|h|th|_t|_th|_h|iso|has|hasKey|qs)\(\s*['"]([a-z][a-zA-Z0-9]*(?:\.[a-zA-Z0-9_]+)+)['"]"""
)
# Namespaces whose leading segment is a catalog namespace; anything else that
# happens to look like `x.y` (e.g. 'image/png') is not a translation key.
_CLIENT_PREFIX_FIXUPS = {"qs": "settings.querySafety."}


def _client_rendered_keys() -> dict[str, set[str]]:
    """Literal catalog keys referenced by the browser runtime, per file."""
    english = _load("en")
    namespaces = {k.split(".", 1)[0] for k in english}
    found: dict[str, set[str]] = {}
    files = [
        p for p in (ROOT / "src" / "static").rglob("*.js")
        if "vendor" not in p.parts and p.name != "i18n.js"
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        keys: set[str] = set()
        for match in _CLIENT_KEY_CALL.finditer(text):
            helper = text[match.start():match.start(1)].split("(")[0].strip()
            key = _CLIENT_PREFIX_FIXUPS.get(helper, "") + match.group(1)
            if key.split(".", 1)[0] in namespaces:
                keys.add(key)
        if keys:
            found[str(path.relative_to(ROOT))] = keys
    return found


def test_client_rendered_keys_exist_in_the_english_catalog():
    """Every literal ``t('ns.key')`` / ``h('ns.key')`` the frontend ships must
    resolve; a typo here renders the raw key to users in every language."""
    english = _load("en")
    per_file = _client_rendered_keys()
    assert len(per_file) >= 10, "expected the frontend producers to be discovered"
    missing = {
        rel: sorted(k for k in keys if k not in english)
        for rel, keys in per_file.items()
    }
    missing = {rel: keys for rel, keys in missing.items() if keys}
    assert missing == {}, f"catalog keys referenced by JS but absent from en.json: {missing}"


def test_translate_falls_back_to_english_then_key():
    assert i18n.translate("he", "login.subtitle") != i18n.translate("en", "login.subtitle")
    assert i18n.translate("he", "does.not.exist") == "does.not.exist"
    assert i18n.translate("en", "login.errors.microsoftFailed", reason="x") == "Microsoft sign-in failed: x"
    assert i18n.isolate("abc") == "\u2068abc\u2069"
    assert i18n.isolate("") == ""


def test_registry_helpers():
    assert i18n.dir_for("he") == "rtl" and i18n.dir_for("he-IL") == "rtl"
    assert i18n.dir_for("en") == "ltr" and i18n.dir_for("") == "ltr"
    assert i18n.normalize_locale("HE_il") == "he"
    assert i18n.normalize_locale("fr") is None
    assert i18n.normalize_locale("<script>") is None
    assert i18n.negotiate_locale("he-IL,he;q=0.9,en;q=0.8") == "he"
    assert i18n.negotiate_locale("fr-FR,de;q=0.5") == "en"
    assert i18n.negotiate_locale("en;q=0.3, he;q=0.9") == "he"
    assert i18n.format_locale_for("he") == "he-IL"
    boot = i18n.client_bootstrap("he")
    assert boot["dir"] == "rtl" and boot["formatLocale"] == "he-IL"
    assert [l["tag"] for l in boot["locales"]] == list(LOCALES)
    assert flatten(boot["messages"]).keys() == _load("en").keys()


def test_resolve_locale_precedence():
    # Signed in: the account value wins over a stale cookie from another user.
    assert i18n.resolve_locale(authenticated=True, session_locale="he", cookie_locale="en") == "he"
    # Signed in without a saved choice: the pre-login cookie drives the display.
    assert i18n.resolve_locale(authenticated=True, session_locale=None, cookie_locale="he") == "he"
    # Anonymous: the session is ignored even if present.
    assert i18n.resolve_locale(authenticated=False, session_locale="he", cookie_locale="en") == "en"
    # Nothing saved: Accept-Language, then the deployment default.
    assert i18n.resolve_locale(authenticated=False, accept_language="he") == "he"
    assert i18n.resolve_locale(authenticated=False, accept_language="fr") == i18n.default_locale()
