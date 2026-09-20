"""Interface language support for the Flask UI.

This module is the ONE place that knows which UI languages the product ships.
Everything else (cookie/DB validation, the language picker, ``<html lang dir>``,
``Intl`` formatting on the client, the Jinja ``t()`` helper) reads from here.

Adding a language
-----------------
1. Add its BCP 47 tag to :data:`LOCALES` and its own-language name to
   :data:`NATIVE_NAMES` (and an ``Intl`` formatting tag to
   :data:`FORMAT_LOCALES` if the region matters, e.g. ``"pt": "pt-BR"``).
2. Add ``src/i18n/messages/<tag>.json`` with the same key set as ``en.json``
   (``tests/unit/test_i18n_catalogs.py`` enforces parity).
3. Only for a new *script*: add a subset font in ``src/static/fonts/`` and put it
   in the ``--font-sans`` stack. Direction is derived from the tag, never stored.

Resolution precedence (see :func:`resolve_locale`)
--------------------------------------------------
* signed in:  account value (``auth_users.locale`` mirrored into the session)
              -> ``locale`` cookie -> ``Accept-Language`` -> ``DEFAULT_LOCALE``
* anonymous:  ``locale`` cookie -> ``Accept-Language`` -> ``DEFAULT_LOCALE``

The cookie is a mirror of the account value plus the pre-login choice; it is
never the authority for a signed-in user, so a previous user's cookie on a
shared browser cannot override the next user's saved language.

Message formats
---------------
Catalogs are ICU MessageFormat, formatted on the client by the vendored
``intl-messageformat``. The few keys rendered server-side (Jinja / Flask
errors) are restricted to plain text plus named ``{arg}`` interpolation so
there is exactly one ICU parser in the product; ``translate()`` implements
only that subset and the catalog test rejects plural/select syntax on the
server-rendered allowlist.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

# ── Registry ─────────────────────────────────────────────────────────────────

#: Shipped UI languages, in picker order. BCP 47 language tags.
LOCALES: tuple[str, ...] = ("en", "he")

#: A language's name in its own language — what the picker shows so every user
#: can find theirs regardless of the current UI language. Hardcoded on purpose:
#: ``Intl.DisplayNames`` is only an enhancement on the client.
NATIVE_NAMES: Mapping[str, str] = {
    "en": "English",
    "he": "עברית",
}

#: ``Intl`` formatting tags per UI tag. UI language and number/date conventions
#: are separate concerns: the catalog uses ``he``, formatters use ``he-IL``.
FORMAT_LOCALES: Mapping[str, str] = {
    "en": "en-US",
    "he": "he-IL",
}

#: Cookie that mirrors the account preference (and holds the pre-login choice).
LOCALE_COOKIE = "locale"
#: One year — the preference is an account setting, not a session flag.
LOCALE_COOKIE_MAX_AGE = 60 * 60 * 24 * 365

_FALLBACK_LOCALE = "en"

# Languages written right-to-left. Portable fallback for runtimes without
# ``Intl.Locale#getTextInfo``; the two agree for every tag shipped here.
_RTL_LANGUAGES = frozenset(
    {"ar", "he", "fa", "ur", "yi", "ps", "dv", "ku", "syr", "ug"}
)

_MESSAGES_DIR = Path(__file__).resolve().parent / "messages"

# A conservative BCP 47 shape: primary language plus up to three short subtags.
_BCP47 = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8}){0,3}$")


def default_locale() -> str:
    """Deployment default from ``DEFAULT_LOCALE`` (validated), else ``en``.

    Read here via the environment (not ``src.config``) because the Flask UI does
    not use the Pydantic settings module.
    """
    return normalize_locale(os.getenv("DEFAULT_LOCALE")) or _FALLBACK_LOCALE


def is_locale(value: Any) -> bool:
    """True when *value* is exactly one of the shipped UI tags."""
    return isinstance(value, str) and value in LOCALES


def normalize_locale(value: Any) -> Optional[str]:
    """Map a loose tag onto a shipped locale, or ``None``.

    ``"he"`` -> ``"he"``; ``"he-IL"`` / ``"HE_il"`` -> ``"he"`` (primary-subtag
    match); anything unknown or malformed -> ``None``. This is the only gate
    between an untrusted cookie/header/body value and the catalog lookup.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 35 or not _BCP47.match(cleaned):
        return None
    tag = cleaned.replace("_", "-")
    lowered = tag.lower()
    for locale in LOCALES:
        if locale.lower() == lowered:
            return locale
    primary = lowered.split("-", 1)[0]
    for locale in LOCALES:
        if locale.lower().split("-", 1)[0] == primary:
            return locale
    return None


def dir_for(locale: str) -> str:
    """Text direction for a BCP 47 tag: ``"rtl"`` or ``"ltr"``."""
    primary = (locale or "").replace("_", "-").split("-", 1)[0].lower()
    return "rtl" if primary in _RTL_LANGUAGES else "ltr"


def format_locale_for(locale: str) -> str:
    """The ``Intl`` tag the client should format numbers/dates with."""
    return FORMAT_LOCALES.get(locale, locale or _FALLBACK_LOCALE)


def native_name(locale: str) -> str:
    return NATIVE_NAMES.get(locale, locale)


def negotiate_locale(accept_language: Optional[str]) -> str:
    """Best shipped locale for an ``Accept-Language`` header.

    Honors ``q`` weights, matches exact tags first (``pt-BR``) then the primary
    language (``pt-PT`` -> ``pt``). Falls back to :func:`default_locale`.
    """
    if not accept_language:
        return default_locale()
    wanted: list[tuple[float, int, str]] = []
    for index, part in enumerate(accept_language.split(",")):
        pieces = [p.strip() for p in part.split(";")]
        tag = pieces[0].lower()
        if not tag or tag == "*":
            continue
        weight = 1.0
        for param in pieces[1:]:
            if param.startswith("q="):
                try:
                    weight = float(param[2:])
                except ValueError:
                    weight = 0.0
        if weight > 0:
            wanted.append((-weight, index, tag))
    wanted.sort()
    shipped = [(loc, loc.lower(), loc.lower().split("-", 1)[0]) for loc in LOCALES]
    for _, _, tag in wanted:
        for locale, lower, _primary in shipped:
            if lower == tag:
                return locale
        primary = tag.split("-", 1)[0]
        for locale, _lower, shipped_primary in shipped:
            if shipped_primary == primary:
                return locale
    return default_locale()


def resolve_locale(
    *,
    authenticated: bool,
    session_locale: Any = None,
    cookie_locale: Any = None,
    accept_language: Optional[str] = None,
) -> str:
    """Pure precedence rule shared by the Flask hooks and the tests.

    For a signed-in user the account value wins; the cookie is consulted only
    when the account has no saved preference (or the user is anonymous).
    """
    if authenticated:
        account = normalize_locale(session_locale)
        if account:
            return account
    cookie = normalize_locale(cookie_locale)
    if cookie:
        return cookie
    return negotiate_locale(accept_language)


# ── Catalogs ─────────────────────────────────────────────────────────────────

Tree = Dict[str, Any]


def _deep_merge(base: Tree, over: Tree) -> Tree:
    out: Tree = dict(base)
    for key, value in over.items():
        existing = out.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            out[key] = _deep_merge(existing, value)
        else:
            out[key] = value
    return out


def _read_catalog(locale: str) -> Tree:
    path = _MESSAGES_DIR / f"{locale}.json"
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: catalog root must be an object")
    return data


@lru_cache(maxsize=None)
def load_messages(locale: str) -> Tree:
    """The catalog for *locale* with English deep-merged underneath.

    A key that is missing or not yet translated renders English rather than a
    raw key path. The parity test still requires every catalog to define the
    full key set, so the merge covers only genuinely in-flight work.
    """
    base = _read_catalog(_FALLBACK_LOCALE)
    if locale == _FALLBACK_LOCALE or not is_locale(locale):
        return base
    return _deep_merge(base, _read_catalog(locale))


def flatten(tree: Mapping[str, Any], prefix: str = "") -> Dict[str, str]:
    """``{"a": {"b": "x"}}`` -> ``{"a.b": "x"}``. Rejects non-string leaves."""
    out: Dict[str, str] = {}
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, str):
            out[path] = value
        elif isinstance(value, Mapping):
            out.update(flatten(value, path))
        else:
            raise ValueError(
                f"{path}: catalogs may only contain strings and nested objects"
            )
    return out


def lookup(messages: Mapping[str, Any], key: str) -> Optional[str]:
    node: Any = messages
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


# ── Server-side formatting (plain text + named args only) ────────────────────

_ARG = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
# Unicode bidi isolates keep an interpolated value (a name, an email, raw
# machine text) from re-ordering the surrounding sentence in the other
# direction. Callers wrap user data explicitly: ``t(key, name=isolate(name))``.
_FSI, _PDI = "\u2068", "\u2069"


def isolate(value: Any) -> str:
    """Wrap *value* in FSI…PDI bidi isolates (empty stays empty)."""
    text = "" if value is None else str(value)
    return f"{_FSI}{text}{_PDI}" if text else text


def format_message(message: str, args: Mapping[str, Any]) -> str:
    """Substitute ``{name}`` placeholders; unknown names are left as-is."""

    def repl(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name not in args:
            return match.group(0)
        value = args[name]
        return "" if value is None else str(value)

    return _ARG.sub(repl, message)


def translate(locale: str, key: str, /, **args: Any) -> str:
    """Server-side ``t()``: catalog lookup with English fallback, then the key."""
    message = lookup(load_messages(locale), key)
    if message is None and locale != _FALLBACK_LOCALE:
        message = lookup(load_messages(_FALLBACK_LOCALE), key)
    if message is None:
        return key
    return format_message(message, args) if args else message


def client_bootstrap(locale: str) -> Dict[str, Any]:
    """Payload embedded in ``index.html`` for ``static/i18n/i18n.js``."""
    return {
        "locale": locale,
        "dir": dir_for(locale),
        "formatLocale": format_locale_for(locale),
        "locales": [
            {"tag": tag, "name": native_name(tag), "dir": dir_for(tag)}
            for tag in LOCALES
        ],
        "messages": load_messages(locale),
    }


def iter_locales() -> Iterable[str]:
    return iter(LOCALES)
