"""Value (entity) linking: match a user-typed literal to a real column value.

Schema linking answers *"which column?"*; value linking answers *"which literal
inside that column?"*. Without it a question like "sales for Mountaiin 300"
produces a filter on a string that exists nowhere in the model, the engine
returns zero rows with HTTP 200, and the user is told "no data" instead of "did
you mean Mountain-300?".

This module is deliberately engine-agnostic — it only ever sees a needle and a
list of candidate strings — so the same matcher can serve the DAX pipeline
(domain fetched with ``VALUES``) and, later, the SQL one (``SELECT DISTINCT``).

Matching uses ``rapidfuzz`` when available and falls back to :mod:`difflib`.
The distinction matters: ``difflib`` is sequence-based and scores
``"Mountaiin 300"`` against ``"Mountain-300 Black, 38"`` poorly because of the
trailing qualifiers, which is exactly the case value linking exists to solve.
The fallback keeps the feature working without the dependency, just with a
lower recall.

Classifying the matches is as important as finding them, because the right
response to "several values matched" depends on *why*:

  refinement  Every match contains the user's phrase plus extra qualifiers
              ("Mountain-300 Black, 38/40/44"). The user meant all of them, so
              widen the filter to ``IN`` and say so — asking which size they
              meant answers a question they did not ask.
  alternative The matches are competing values ("Mountain-300" vs
              "Mountain-500"). Only the user can pick, so ask.

A single-token needle is never treated as a refinement: "Bike" legitimately
covers "Bikes", "Bike Racks" and "Bike Stands", and silently summing all three
would be wrong. Two or more tokens make accidental coverage unlikely.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

try:  # pragma: no cover - exercised by whichever backend is installed
    from rapidfuzz import fuzz as _fuzz

    def _partial_ratio(a: str, b: str) -> float:
        return float(_fuzz.partial_ratio(a, b))

    def _token_set_ratio(a: str, b: str) -> float:
        return float(_fuzz.token_set_ratio(a, b))

    def _plain_ratio(a: str, b: str) -> float:
        return float(_fuzz.ratio(a, b))

except ImportError:  # pragma: no cover - fallback path
    from difflib import SequenceMatcher

    def _plain_ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio() * 100.0

    def _partial_ratio(a: str, b: str) -> float:
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        if not shorter:
            return 0.0
        window = len(shorter)
        best = 0.0
        for start in range(0, max(1, len(longer) - window + 1)):
            best = max(best, _plain_ratio(shorter, longer[start : start + window]))
            if best >= 100.0:
                break
        return best

    def _token_set_ratio(a: str, b: str) -> float:
        ta, tb = set(a.split()), set(b.split())
        if not ta or not tb:
            return 0.0
        return _plain_ratio(" ".join(sorted(ta)), " ".join(sorted(tb)))


_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")
_HAS_DIGIT_RE = re.compile(r"\d")

# Score at or above which a domain value is worth showing the user at all.
DEFAULT_MATCH_THRESHOLD = 78.0
# Score at or above which one needle token is considered present in a candidate.
DEFAULT_TOKEN_THRESHOLD = 82.0


def normalize(value: object) -> str:
    """Lower-case, strip punctuation and collapse whitespace.

    ``"Mountain-300 Black, 38"`` -> ``"mountain 300 black 38"`` so hyphenation
    and comma style never decide a match.
    """
    text = str(value or "").strip().lower()
    return _NON_ALNUM_RE.sub(" ", text).strip()


def tokenize(value: object) -> List[str]:
    normalized = normalize(value)
    return normalized.split() if normalized else []


@dataclass(frozen=True)
class ValueMatch:
    """One domain value scored against the user's literal."""

    value: str
    score: float
    # True when every token of the needle appears (fuzzily) in this value, i.e.
    # the candidate is the user's phrase plus extra qualifiers.
    covers_needle: bool


def score_value(needle_norm: str, candidate_norm: str) -> float:
    """Best of substring and token-set similarity, 0-100."""
    if not needle_norm or not candidate_norm:
        return 0.0
    return max(
        _partial_ratio(needle_norm, candidate_norm),
        _token_set_ratio(needle_norm, candidate_norm),
    )


def token_matches(
    needle_token: str,
    candidate_token: str,
    token_threshold: float = DEFAULT_TOKEN_THRESHOLD,
) -> bool:
    """True when two tokens are the same token, allowing for a typo.

    Tokens containing a digit must match **exactly**. In a product catalogue the
    number is the discriminator, not a spelling detail: ``"300"`` scores 86
    against ``"3000"``, which is well above any useful typo threshold, so fuzzy
    matching there would fold "Mountain-3000" into a query for "Mountain 300"
    and silently over-report. A wrong number is a different product; a wrong
    letter is usually a typo.
    """
    if _HAS_DIGIT_RE.search(needle_token) or _HAS_DIGIT_RE.search(candidate_token):
        return needle_token == candidate_token
    return _plain_ratio(needle_token, candidate_token) >= token_threshold


def covers_all_tokens(
    needle_tokens: Sequence[str],
    candidate_tokens: Sequence[str],
    token_threshold: float = DEFAULT_TOKEN_THRESHOLD,
) -> bool:
    """True when each needle token matches some token of the candidate."""
    if not needle_tokens or not candidate_tokens:
        return False
    for token in needle_tokens:
        if not any(token_matches(token, other, token_threshold) for other in candidate_tokens):
            return False
    return True


def exact_value(needle: str, domain: Sequence[str]) -> Optional[str]:
    """Return the domain value equal to *needle* ignoring case/punctuation."""
    needle_norm = normalize(needle)
    if not needle_norm:
        return None
    for value in domain:
        if normalize(value) == needle_norm:
            return value
    return None


def match_values(
    needle: str,
    domain: Sequence[str],
    *,
    limit: int = 25,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    token_threshold: float = DEFAULT_TOKEN_THRESHOLD,
) -> List[ValueMatch]:
    """Score *domain* against *needle*, best first.

    Only values scoring at or above *threshold* are returned, capped at *limit*.
    """
    needle_norm = normalize(needle)
    if not needle_norm or not domain:
        return []
    needle_tokens = needle_norm.split()

    scored: List[ValueMatch] = []
    seen: set = set()
    for value in domain:
        text = str(value)
        candidate_norm = normalize(text)
        if not candidate_norm or candidate_norm in seen:
            continue
        seen.add(candidate_norm)
        score = score_value(needle_norm, candidate_norm)
        if score < threshold:
            continue
        scored.append(
            ValueMatch(
                value=text,
                score=score,
                covers_needle=covers_all_tokens(
                    needle_tokens, candidate_norm.split(), token_threshold
                ),
            )
        )

    # Coverage first, then score: a refinement of the user's phrase is always a
    # better answer than a higher-scoring value that drops one of their tokens.
    scored.sort(key=lambda m: (m.covers_needle, m.score), reverse=True)
    return scored[:limit]


def is_refinement_set(needle: str, matches: Sequence[ValueMatch]) -> bool:
    """True when *matches* are the user's phrase plus qualifiers, not rivals.

    Requires a multi-token needle — see the module docstring for why a single
    token ("Bike") must never be widened automatically.
    """
    if len(matches) < 2 or len(tokenize(needle)) < 2:
        return False
    return all(m.covers_needle for m in matches)


def search_tokens(needle: str, *, prefix_length: int = 4, max_tokens: int = 4) -> List[str]:
    """Server-side search fragments for a needle that may contain a typo.

    Each token is truncated to *prefix_length* so a misspelling later in the
    word ("Mountaiin") still matches; short tokens ("300") are kept whole
    because they are usually the discriminating part.
    """
    fragments: List[str] = []
    for token in tokenize(needle):
        fragment = token if len(token) <= prefix_length else token[:prefix_length]
        if fragment and fragment not in fragments:
            fragments.append(fragment)
    return fragments[:max_tokens]


# ── Cached value domains ──────────────────────────────────────────────────────


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "")))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ValueDomain:
    """A snapshot of a column's distinct values.

    ``complete`` is False when the column had more distinct values than the
    fetch cap, meaning "not in ``values``" does not prove "not in the column".
    """

    values: Tuple[str, ...]
    complete: bool


class ValueDomainCache:
    """Thread-safe TTL + LRU cache of column value domains.

    Keyed **per user and per scope**: domains are fetched with the signed-in
    user's delegated token, so row-level security can make them differ between
    users, and the scope carries whatever else must not be shared — the dataset
    and the grant identity behind the token. Sharing an entry across any of
    those boundaries would surface values the reader may not see.

    Two caveats the key cannot cover. Callers must confirm the reader is still
    entitled *before* reading the cache, because a hit performs no authorization
    of its own. And a row-level-security rule changed inside Power BI is only
    picked up when the entry expires, so the TTL bounds how stale a value list
    can be; it is deliberately short for that reason.
    """

    _SEP = "\x00"

    def __init__(self, max_entries: int = 256, ttl_seconds: int = 900) -> None:
        self._max = max_entries
        self._ttl = ttl_seconds
        self._store: "OrderedDict[str, Tuple[float, ValueDomain]]" = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def key(
        cls,
        source_key: object,
        user_id: object,
        table: object,
        column: object,
        scope: object = "",
    ) -> str:
        """Build a cache key, or ``""`` when it would not be safe to cache.

        An empty ``user_id`` yields no key: caching under a blank identity would
        let unrelated readers share one RLS-filtered domain.
        """
        parts = [
            str(source_key or "").strip().lower(),
            str(scope or "").strip().lower(),
            str(user_id or "").strip(),
            str(table or "").strip().lower(),
            str(column or "").strip().lower(),
        ]
        if not parts[2] or not parts[3] or not parts[4]:
            return ""
        return cls._SEP.join(parts)

    def get(self, key: str) -> Optional[ValueDomain]:
        if not key:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            inserted, domain = entry
            if now - inserted > self._ttl:
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            return domain

    def put(self, key: str, domain: ValueDomain) -> None:
        if not key:
            return
        now = time.monotonic()
        with self._lock:
            expired = [k for k, (ts, _) in self._store.items() if now - ts > self._ttl]
            for k in expired:
                self._store.pop(k, None)
            self._store[key] = (now, domain)
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"entries": len(self._store), "max_entries": self._max, "ttl": self._ttl}


value_domain_cache = ValueDomainCache(
    max_entries=_env_int("JEEN_VALUE_DOMAIN_CACHE_MAX_ENTRIES", 256),
    ttl_seconds=_env_int("JEEN_VALUE_DOMAIN_CACHE_TTL_SECONDS", 900),
)


__all__ = [
    "DEFAULT_MATCH_THRESHOLD",
    "DEFAULT_TOKEN_THRESHOLD",
    "ValueDomain",
    "ValueDomainCache",
    "ValueMatch",
    "covers_all_tokens",
    "exact_value",
    "is_refinement_set",
    "match_values",
    "normalize",
    "score_value",
    "search_tokens",
    "token_matches",
    "tokenize",
    "value_domain_cache",
]
