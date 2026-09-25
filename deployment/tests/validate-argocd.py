#!/usr/bin/env python3
"""Validate the credential-free Argo CD examples and migration guardrails."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


ROOT = Path(__file__).resolve().parents[2]
ARGO_DIR = ROOT / "deployment" / "argocd"
PINNED_REVISION = re.compile(r"^[0-9a-f]{40}$")
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
IMMUTABLE_TAG = re.compile(r"^[a-z0-9][a-z0-9._-]*[0-9a-f]{7,40}$")

REQUIRED_SYNC_OPTIONS = {
    "CreateNamespace=true",
    "PruneLast=true",
    "FailOnSharedResource=true",
}


def fail(errors: list[str], path: Path, message: str) -> None:
    errors.append(f"{path.relative_to(ROOT)}: {message}")


def load_yaml(errors: list[str]) -> dict[Path, list[dict[str, Any]]]:
    loaded: dict[Path, list[dict[str, Any]]] = {}
    paths = sorted((*ARGO_DIR.rglob("*.yaml"), *ARGO_DIR.rglob("*.yml")))
    if not paths:
        errors.append("deployment/argocd contains no YAML examples")
        return loaded

    for path in paths:
        try:
            raw_documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except yaml.YAMLError as exc:
            fail(errors, path, f"invalid YAML: {exc}")
            continue
        documents: list[dict[str, Any]] = []
        for index, document in enumerate(raw_documents, start=1):
            if document is None:
                continue
            if not isinstance(document, dict):
                fail(errors, path, f"document {index} is not a mapping")
                continue
            documents.append(document)
        if not documents:
            fail(errors, path, "contains no YAML documents")
        loaded[path] = documents
    return loaded


def walk(value: Any) -> list[tuple[str, Any]]:
    entries: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            entries.append((str(key), child))
            entries.extend(walk(child))
    elif isinstance(value, list):
        for child in value:
            entries.extend(walk(child))
    return entries


def validate_no_secret_payloads(
    errors: list[str], loaded: dict[Path, list[dict[str, Any]]]
) -> None:
    for path, documents in loaded.items():
        for document in documents:
            if document.get("kind") == "Secret":
                fail(errors, path, "must not embed a Kubernetes Secret")
            for key, _ in walk(document):
                if key in {"data", "stringData"}:
                    fail(errors, path, f"must not embed secret payload key {key!r}")

            annotations = document.get("metadata", {}).get("annotations", {})
            if "helm.sh/hook" in annotations or "argocd.argoproj.io/hook" in annotations:
                fail(errors, path, "migration examples must not use Helm/Argo hooks")


def application_map(
    errors: list[str], loaded: dict[Path, list[dict[str, Any]]]
) -> dict[str, tuple[Path, dict[str, Any]]]:
    applications: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path, documents in loaded.items():
        for document in documents:
            if document.get("kind") != "Application":
                continue
            name = document.get("metadata", {}).get("name")
            if not isinstance(name, str) or not name:
                fail(errors, path, "Application is missing metadata.name")
                continue
            if name in applications:
                fail(errors, path, f"duplicate Application name {name!r}")
            applications[name] = (path, document)
    return applications


def require_example_url(
    errors: list[str], path: Path, value: Any, description: str
) -> None:
    if not isinstance(value, str):
        fail(errors, path, f"{description} must be a URL string")
        return
    host = urlparse(value).hostname or ""
    if not host.endswith(".example.invalid"):
        fail(errors, path, f"{description} must use an example.invalid host")


def validate_application_placeholders(
    errors: list[str], applications: dict[str, tuple[Path, dict[str, Any]]]
) -> None:
    for name, (path, application) in applications.items():
        spec = application.get("spec", {})
        source = spec.get("source", {})
        destination = spec.get("destination", {})
        require_example_url(errors, path, source.get("repoURL"), f"{name} repoURL")
        require_example_url(
            errors, path, destination.get("server"), f"{name} destination.server"
        )
        revision = source.get("targetRevision")
        if not isinstance(revision, str) or not PINNED_REVISION.fullmatch(revision):
            fail(errors, path, f"{name} targetRevision must be a pinned 40-hex commit")


def sync_wave(application: dict[str, Any]) -> str | None:
    return application.get("metadata", {}).get("annotations", {}).get(
        "argocd.argoproj.io/sync-wave"
    )


def sync_options(application: dict[str, Any]) -> set[str]:
    options = application.get("spec", {}).get("syncPolicy", {}).get(
        "syncOptions", []
    )
    return {option for option in options if isinstance(option, str)}


def require_sync_options(
    errors: list[str], path: Path, application: dict[str, Any]
) -> None:
    missing = REQUIRED_SYNC_OPTIONS - sync_options(application)
    if missing:
        fail(errors, path, f"missing sync options {sorted(missing)}")


def helm_values(
    errors: list[str], path: Path, application: dict[str, Any]
) -> dict[str, Any]:
    raw = (
        application.get("spec", {})
        .get("source", {})
        .get("helm", {})
        .get("values")
    )
    if not isinstance(raw, str):
        fail(errors, path, "Application must provide inline Helm values")
        return {}
    try:
        values = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        fail(errors, path, f"inline Helm values are invalid YAML: {exc}")
        return {}
    if not isinstance(values, dict):
        fail(errors, path, "inline Helm values must be a mapping")
        return {}
    return values


def validate_image(
    errors: list[str], path: Path, image: Any, description: str
) -> None:
    if not isinstance(image, dict):
        fail(errors, path, f"{description} image must be a mapping")
        return
    repository = image.get("repository")
    require_example_url(
        errors,
        path,
        f"https://{repository}" if isinstance(repository, str) else repository,
        f"{description} image repository",
    )
    digest = image.get("digest")
    tag = image.get("tag")
    if isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        return
    if not isinstance(tag, str) or tag.lower() == "latest":
        fail(errors, path, f"{description} image must use an immutable non-latest tag")
        return
    if any(marker in tag.lower() for marker in ("replace", "placeholder")):
        fail(errors, path, f"{description} image tag is still a placeholder")
    elif not IMMUTABLE_TAG.fullmatch(tag):
        fail(
            errors,
            path,
            f"{description} image tag must end in a 7-40 character lowercase hex ID",
        )


def validate_project(
    errors: list[str], loaded: dict[Path, list[dict[str, Any]]]
) -> None:
    projects = [
        (path, document)
        for path, documents in loaded.items()
        for document in documents
        if document.get("kind") == "AppProject"
    ]
    if len(projects) != 1:
        errors.append(f"expected exactly one AppProject, found {len(projects)}")
        return
    path, project = projects[0]
    if sync_wave(project) != "-3":
        fail(errors, path, "AppProject must use sync wave -3")
    spec = project.get("spec", {})
    source_repos = spec.get("sourceRepos", [])
    if not source_repos or "*" in source_repos:
        fail(errors, path, "AppProject must use exact source repositories")
    for repo in source_repos:
        require_example_url(errors, path, repo, "AppProject source repository")
    destinations = spec.get("destinations", [])
    if len(destinations) != 1:
        fail(errors, path, "AppProject must have one least-privilege destination")
    for destination in destinations:
        require_example_url(
            errors, path, destination.get("server"), "AppProject destination"
        )
        namespace = destination.get("namespace")
        if not isinstance(namespace, str) or "*" in namespace:
            fail(errors, path, "AppProject destination namespace must be exact")

    allowed_kinds = {
        item.get("kind")
        for item in (
            spec.get("clusterResourceWhitelist", [])
            + spec.get("namespaceResourceWhitelist", [])
        )
        if isinstance(item, dict)
    }
    if "Secret" in allowed_kinds or "*" in allowed_kinds:
        fail(errors, path, "AppProject must not allow Secret or wildcard resources")
    for required in ("Namespace", "Job", "ExternalSecret", "SecretStore"):
        if required not in allowed_kinds:
            fail(errors, path, f"AppProject does not allow required {required} resources")


def validate_phases(
    errors: list[str], applications: dict[str, tuple[Path, dict[str, Any]]]
) -> None:
    required = {
        "jeen-insights-example-bootstrap",
        "jeen-insights-lower-secret-bootstrap",
        "jeen-insights-lower-migration",
        "jeen-insights-lower-workloads",
    }
    missing = required - applications.keys()
    if missing:
        errors.append(f"missing required Applications: {sorted(missing)}")
        return

    bootstrap_path, bootstrap = applications["jeen-insights-example-bootstrap"]
    if bootstrap.get("spec", {}).get("source", {}).get("path") != "deployment/argocd":
        fail(errors, bootstrap_path, "bootstrap Application must source deployment/argocd")

    secret_path, secret = applications["jeen-insights-lower-secret-bootstrap"]
    if sync_wave(secret) != "-2":
        fail(errors, secret_path, "secret-bootstrap Application must use sync wave -2")
    require_sync_options(errors, secret_path, secret)
    secret_source_path = secret.get("spec", {}).get("source", {}).get("path", "")
    if "operator-owned" not in secret_source_path or "secret" not in secret_source_path:
        fail(
            errors,
            secret_path,
            "secret-bootstrap must reference operator-owned secret-store manifests",
        )

    migration_path, migration = applications["jeen-insights-lower-migration"]
    if sync_wave(migration) != "-1":
        fail(errors, migration_path, "migration Application must use sync wave -1")
    require_sync_options(errors, migration_path, migration)
    validate_migration(errors, migration_path, migration)

    workload_path, workload = applications["jeen-insights-lower-workloads"]
    if sync_wave(workload) != "0":
        fail(errors, workload_path, "workload Application must use sync wave 0")
    require_sync_options(errors, workload_path, workload)
    validate_workload(errors, workload_path, workload)


def validate_migration(
    errors: list[str], path: Path, application: dict[str, Any]
) -> None:
    spec = application.get("spec", {})
    source = spec.get("source", {})
    if source.get("path") != "deployment/k8s_dev":
        fail(errors, path, "migration must source the canonical deployment/k8s_dev chart")
    sync_policy = spec.get("syncPolicy", {})
    if "automated" in sync_policy:
        fail(errors, path, "migration must not enable automated sync or self-heal")
    options = sync_options(application)
    if "ApplyOutOfSyncOnly=true" not in options:
        fail(errors, path, "migration must set ApplyOutOfSyncOnly=true")
    if {"Replace=true", "Force=true"} & options:
        fail(errors, path, "migration must not force or replace a completed Job")

    values = helm_values(errors, path, application)
    external_secrets = values.get("global", {}).get("externalSecrets", {})
    if external_secrets.get("enabled") is not False:
        fail(errors, path, "migration values must disable ExternalSecret rendering")
    for component in (
        "jeen-insights-api",
        "jeen-insights-ui",
        "jeen-insights-analytics",
    ):
        if values.get(component, {}).get("enabled") is not False:
            fail(errors, path, f"migration values must disable {component}")

    migration = values.get("migration", {})
    if migration.get("enabled") is not True:
        fail(errors, path, "migration.enabled must be true")
    if "ttlSecondsAfterFinished" not in migration:
        fail(errors, path, "migration must explicitly set ttlSecondsAfterFinished")
    elif migration.get("ttlSecondsAfterFinished") is not None:
        fail(errors, path, "migration ttlSecondsAfterFinished must be null")
    bundle_id = migration.get("bundleId")
    if not isinstance(bundle_id, str) or not DNS_LABEL.fullmatch(bundle_id):
        fail(errors, path, "migration bundleId must be a DNS-safe immutable identifier")
    elif any(marker in bundle_id for marker in ("replace", "placeholder")):
        fail(errors, path, "migration bundleId is still a placeholder")
    validate_image(errors, path, migration.get("image"), "migration")


def validate_workload(
    errors: list[str], path: Path, application: dict[str, Any]
) -> None:
    spec = application.get("spec", {})
    source = spec.get("source", {})
    if source.get("path") != "deployment/k8s_dev":
        fail(errors, path, "workloads must source the canonical deployment/k8s_dev chart")
    if "automated" in spec.get("syncPolicy", {}):
        fail(
            errors,
            path,
            "default workload example must require manual promotion after migration",
        )

    values = helm_values(errors, path, application)
    if values.get("migration", {}).get("enabled") is not False:
        fail(errors, path, "workload values must set migration.enabled=false")
    workload_env = values.get("global", {}).get("env", {})
    for setting in ("RUN_MIGRATIONS_ON_START", "SCHEMA_BOOTSTRAP_ON_START"):
        if workload_env.get(setting) != "false":
            fail(errors, path, f"workload values must set {setting}=false")
    for component in (
        "jeen-insights-api",
        "jeen-insights-ui",
        "jeen-insights-analytics",
    ):
        validate_image(errors, path, values.get(component, {}).get("image"), component)


def validate_kustomization(
    errors: list[str], loaded: dict[Path, list[dict[str, Any]]]
) -> None:
    path = ARGO_DIR / "kustomization.yaml"
    documents = loaded.get(path, [])
    if len(documents) != 1 or documents[0].get("kind") != "Kustomization":
        fail(errors, path, "must contain exactly one Kustomization")
        return
    resources = set(documents[0].get("resources", []))
    required = {
        "project.yaml",
        "applications/secret-bootstrap.yaml",
        "applications/migration.yaml",
        "applications/workloads-lower.yaml",
    }
    missing = required - resources
    if missing:
        fail(errors, path, f"missing bootstrap resources {sorted(missing)}")


def main() -> int:
    errors: list[str] = []
    loaded = load_yaml(errors)
    validate_no_secret_payloads(errors, loaded)
    applications = application_map(errors, loaded)
    validate_application_placeholders(errors, applications)
    validate_project(errors, loaded)
    validate_phases(errors, applications)
    validate_kustomization(errors, loaded)

    if errors:
        print("Argo CD validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Argo CD validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
