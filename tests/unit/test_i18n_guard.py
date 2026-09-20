"""Scoped guard against NEW hardcoded English in the v3 interface producers.

Every user-visible string in these files must come from the locale catalog
(``window.I18n.t`` / ``h``). The heuristics below look only at UI sinks
(toasts, confirms, ``textContent``/``title``/``placeholder`` assignments,
attribute literals and text between tags in template literals), so SQL,
prompts, logs and identifiers are never flagged.

Deliberately NOT repository-wide: the legacy ``script.js`` / ``chatController.js``
paths and the public landing page are out of scope for the first release.

A line may opt out with an ``// i18n-ignore`` comment (developer-only text).
``BASELINE`` lists the admin-only MCP / integrations tooling copy in
``settingsPage.js`` that is still English; shrink it as those panels are
localised, never grow it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

GUARDED = [
    "src/static/workspace/workspaceController.js",
    "src/static/workspace/onboarding.js",
    "src/static/analysis/analysisPanel.js",
    "src/static/insights/insightsManager.js",
    "src/static/auth.js",
    "src/static/settings/settingsPage.js",
    "src/static/chart-feature/chartManager.js",
    "src/static/chart-feature/components/ChartChat.js",
    "src/static/chart-feature/components/ChartContainer.js",
    "src/static/chart-feature/components/ChartOptionsPanel.js",
    "src/static/chart-feature/components/ChartToggle.js",
    "src/static/chart-feature/components/ChartTypeSelector.js",
    "src/static/chart-feature/components/EnhanceButton.js",
    "src/static/chart-feature/components/MapOptionsPanel.js",
]

PATTERNS = [
    re.compile(r"""(?:show[Tt]oast|_showToast|confirm|prompt|alert)\(\s*(['"`])([A-Z][^'"`$]{3,})\1"""),
    re.compile(r"""\.(?:textContent|title|placeholder|innerText)\s*=\s*(['"])([A-Z][a-z][^'"]{2,})\1"""),
    re.compile(r"""(?:aria-label|title|placeholder|data-tooltip)="([A-Z][a-z][^"$<>]{2,})\""""),
    re.compile(r""">([A-Z][a-z]+(?: [a-zA-Z(){}/&.…'-]+){1,}[.…?!]?)<"""),
    re.compile(r"""new Error\(\s*(['"`])([A-Z][^'"`$]{3,})\1"""),
    # Text trailing a tag or interpolation on the same line: `</svg> Add server`, `${icon} Checking…`
    re.compile(r"""(?:</[a-z]+>|\})\s+([A-Z][a-z]+(?: [a-zA-Z(){}/&.…'-]+){1,}[.…?!]?)\s*(?:<|$|`)"""),
    # Emoji/glyph-prefixed labels: `📊 Table`, `✨ Enhance with AI`, `✓ Enhanced!`
    re.compile(r"""[\u2190-\u27bf\U0001f300-\U0001faff]\s+([A-Z][a-z]+(?: [a-zA-Z!.…'-]+)*[!.…]?)\s*(?:<|'|`|$)"""),
]

# Admin-only MCP catalog / integrations / tool-inspector copy in settingsPage.js
# that is still English (see module docstring). Snippet text only, not lines.
BASELINE = {
    # Empty: every guarded producer is fully catalog-driven. Add entries only
    # with a reason, and remove them as soon as the copy is localised.
}


def _hits(path: Path) -> set[str]:
    found: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if "i18n-ignore" in line:
            continue
        for pattern in PATTERNS:
            for match in pattern.finditer(line):
                found.add(match.group(match.lastindex).strip())
    return found


@pytest.mark.parametrize("rel", GUARDED)
def test_no_new_hardcoded_english_in_v3_interface_producers(rel):
    hits = _hits(ROOT / rel)
    allowed = BASELINE.get(rel, set())
    new = sorted(hits - allowed)
    assert new == [], (
        f"{rel}: hardcoded interface copy found — move it to src/i18n/messages/*.json and "
        f"render it with I18n.t()/h() (or mark developer-only text with // i18n-ignore): {new}"
    )


def test_baseline_only_shrinks():
    """A baseline entry that no longer matches has been localised: remove it."""
    stale = {}
    for rel, allowed in BASELINE.items():
        hits = _hits(ROOT / rel)
        unused = sorted(allowed - hits)
        if unused:
            stale[rel] = unused
    # Entries listed defensively (e.g. labels that share a line with an
    # interpolation) are tolerated, but the bulk of the baseline must be live.
    for rel, unused in stale.items():
        assert len(unused) < len(BASELINE[rel]) * 0.6, f"{rel}: baseline is mostly stale: {unused}"


def test_catalog_messages_are_not_used_as_html():
    """`t()` results must never be assigned to innerHTML unescaped with user args.

    The convention is `h()` (escaped) inside templates; `t()` only feeds
    textContent/attributes via escaping helpers. This spot-checks the pattern.
    """
    pattern = re.compile(r"""innerHTML\s*=\s*(?:window\.I18n\.)?t\(""")
    offenders = [rel for rel in GUARDED if pattern.search((ROOT / rel).read_text(encoding="utf-8"))]
    assert offenders == []
