#!/usr/bin/env python3
"""Validate deployment documentation structure, links, and env coverage."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT = ROOT / "deployment"
CONFIGURATION = DEPLOYMENT / "configuration.md"

REQUIRED_FILES = (
    DEPLOYMENT / "README.md",
    CONFIGURATION,
    DEPLOYMENT / "migrations.md",
    DEPLOYMENT / "oidc.md",
    DEPLOYMENT / "argo.md",
    DEPLOYMENT / "argocd" / "README.md",
    DEPLOYMENT / "aks" / "INSTALL.md",
    DEPLOYMENT / "eks" / "INSTALL.md",
    DEPLOYMENT / "openshift" / "INSTALL.md",
)

SIX_STEPS = (
    "1. prepare registry",
    "2. configure values and secrets",
    "3. preflight",
    "4. run migration",
    "5. install or upgrade",
    "6. verify",
)

CONFIG_HEADINGS = (
    "required database",
    "deployment security",
    "migration controls",
    "service and public url",
    "llm",
    "microsoft entra and generic oidc",
    "ml sandbox",
    "maps and geocoding",
    "query, dlp, concurrency, memory and persistence",
    "dax, power bi and filter grounding",
    "connectors",
    "legacy and runtime-only variables",
)

ARGO_HEADINGS = (
    "ownership boundary",
    "prerequisites",
    "appproject and least-practical access",
    "bootstrap and phase order",
    "secret ownership",
    "migration application: wave -1",
    "workload application: wave 0",
    "sync, health, and rollback",
)

# Variables may be added here only when .env.example intentionally documents a
# non-runtime fixture. Keep the reason beside each entry.
ENV_COVERAGE_IGNORE: dict[str, str] = {}

# Canonical configuration-table variables intentionally omitted from the
# copy-and-run local .env template. Keep entries explicit and categorized so a
# newly documented deployment variable cannot silently escape the inventory.
DOCS_ONLY_VARIABLES = {
    # Container/runtime locations and listeners are supplied by images/charts.
    "ANALYTICS_HOST",
    "ANALYTICS_PORT",
    "HOME",
    "MPLCONFIGDIR",
    # Advanced DAX/Power BI/filter-grounding tuning uses application defaults.
    "DAX_ENTITY_CROSS_COLUMN_ENABLED",
    "DAX_ENTITY_MATCH_THRESHOLD",
    "DAX_ENTITY_MAX_DOMAIN_VALUES",
    "DAX_ENTITY_RESOLUTION_ENABLED",
    "DAX_MAX_RETRIES",
    "DAX_VALIDATION_ENABLED",
    "POWERBI_API_BASE",
    "POWERBI_DEFAULT_SCOPE",
    "POWERBI_EXECUTE_TIMEOUT_SECONDS",
    "SQL_FILTER_ABSENCE_MAX_AGE_HOURS",
    "SQL_FILTER_CACHE_TTL_SECONDS",
    "SQL_FILTER_EXISTENCE_MAX_AGE_HOURS",
    "SQL_FILTER_LOOKUP_TIMEOUT_MS",
    "SQL_FILTER_MATCH_THRESHOLD",
    "SQL_FILTER_MAX_DOMAIN_VALUES",
    "SQL_FILTER_METADATA_DB_FALLBACK",
    "SQL_FILTER_METADATA_EVIDENCE_ENABLED",
    "SQL_FILTER_PROBE_DENYLIST",
    "SQL_FILTER_RESOLUTION_ENABLED",
    "SQL_FILTER_SOURCE_DISTINCT_ENABLED",
    "SQL_FILTER_SOURCE_PROBE_ENABLED",
    "SQL_FILTER_UNVERIFIED_EXECUTION",
    "SQL_FILTER_VALUE_VISIBILITY",
    # Optional scale/schema-link and connector fallback tuning.
    "MAX_CONCURRENT_QUERIES_PER_USER",
    "QUERY_QUEUE_WAIT_SECONDS",
    "SCHEMA_LINK_ENABLED",
    "SCHEMA_LINK_MAX_COLUMNS",
    "SCHEMA_LINK_MAX_COLUMNS_PER_TABLE",
    "SCHEMA_LINK_MAX_TABLES",
    "SCHEMA_LINK_MIN_COLUMNS",
    "JIRA_CLIENT_ID",
    "SLACK_CLIENT_ID",
}

LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
ENV_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)
CONFIG_TABLE_ENV_RE = re.compile(
    r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|", re.MULTILINE
)
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def check_required_files(errors: list[str]) -> None:
    for path in REQUIRED_FILES:
        if not path.is_file():
            errors.append(f"missing required file: {path.relative_to(ROOT)}")


def headings(path: Path) -> list[str]:
    return [match.group(1).strip().lower() for match in HEADING_RE.finditer(path.read_text())]


def check_headings(errors: list[str]) -> None:
    for path in (
        DEPLOYMENT / "README.md",
        DEPLOYMENT / "aks" / "INSTALL.md",
        DEPLOYMENT / "eks" / "INSTALL.md",
        DEPLOYMENT / "openshift" / "INSTALL.md",
    ):
        if not path.is_file():
            continue
        actual = headings(path)
        for required in SIX_STEPS:
            if not any(heading.startswith(required) for heading in actual):
                errors.append(
                    f"{path.relative_to(ROOT)} missing numbered heading beginning "
                    f"with {required!r}"
                )

    if CONFIGURATION.is_file():
        actual = headings(CONFIGURATION)
        for required in CONFIG_HEADINGS:
            if required not in actual:
                errors.append(
                    f"{CONFIGURATION.relative_to(ROOT)} missing heading {required!r}"
                )

    argo_readme = DEPLOYMENT / "argocd" / "README.md"
    if argo_readme.is_file():
        actual = headings(argo_readme)
        for required in ARGO_HEADINGS:
            if required not in actual:
                errors.append(
                    f"{argo_readme.relative_to(ROOT)} missing heading {required!r}"
                )


def link_target(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1]
    return unquote(raw.split("#", 1)[0].split("?", 1)[0])


def check_links(errors: list[str]) -> None:
    for document in sorted(DEPLOYMENT.rglob("*.md")):
        text = document.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            raw = match.group(1).strip()
            if (
                not raw
                or raw.startswith("#")
                or re.match(r"^[a-z][a-z0-9+.-]*:", raw, re.IGNORECASE)
            ):
                continue
            target_text = link_target(raw)
            target = (document.parent / target_text).resolve()
            try:
                target.relative_to(ROOT)
            except ValueError:
                errors.append(
                    f"{document.relative_to(ROOT)} link escapes repository: {raw}"
                )
                continue
            if not target.exists():
                line = text.count("\n", 0, match.start()) + 1
                errors.append(
                    f"{document.relative_to(ROOT)}:{line} dangling local link: {raw}"
                )


def check_env_coverage(errors: list[str]) -> None:
    env_path = ROOT / ".env.example"
    if not env_path.is_file() or not CONFIGURATION.is_file():
        return
    variables = set(ENV_RE.findall(env_path.read_text(encoding="utf-8")))
    configuration = CONFIGURATION.read_text(encoding="utf-8")
    documented = set(CONFIG_TABLE_ENV_RE.findall(configuration))
    missing = sorted(variables - documented - ENV_COVERAGE_IGNORE.keys())
    for variable in missing:
        errors.append(
            f".env.example variable {variable} is absent from "
            "deployment/configuration.md and ENV_COVERAGE_IGNORE"
        )
    absent_from_env = sorted(documented - variables - DOCS_ONLY_VARIABLES)
    for variable in absent_from_env:
        errors.append(
            f"deployment/configuration.md variable {variable} is absent from "
            ".env.example and DOCS_ONLY_VARIABLES"
        )


def main() -> int:
    errors: list[str] = []
    check_required_files(errors)
    check_headings(errors)
    check_links(errors)
    check_env_coverage(errors)
    if errors:
        print("Deployment documentation validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Deployment documentation validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
