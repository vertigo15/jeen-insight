#!/usr/bin/env python3
"""Semantic checks for manifests rendered from deployment/k8s_dev."""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path
from typing import Any

import yaml


PROFILE_CONTRACTS = {
    "base": {
        "deployments": 2,
        "required": {"Deployment", "Service"},
        "forbidden": {"ExternalSecret", "Ingress", "Route", "Secret"},
    },
    "aks-dev": {
        "deployments": 2,
        "required": {"Deployment", "ExternalSecret", "Ingress", "Service"},
        "forbidden": {"Route", "Secret"},
    },
    "aks-example": {
        "deployments": 3,
        "required": {
            "Deployment",
            "ExternalSecret",
            "Ingress",
            "NetworkPolicy",
            "Service",
        },
        "forbidden": {"Route", "Secret"},
    },
    "defence": {
        "deployments": 3,
        "required": {
            "Deployment",
            "Gateway",
            "HTTPRoute",
            "NetworkPolicy",
            "SecretProviderClass",
            "Service",
        },
        "forbidden": {"ExternalSecret", "Ingress", "Route", "Secret"},
    },
    "eks-example": {
        "deployments": 3,
        "required": {
            "Deployment",
            "ExternalSecret",
            "Ingress",
            "NetworkPolicy",
            "Service",
        },
        "forbidden": {"Route", "Secret"},
    },
    "openshift": {
        "deployments": 3,
        "required": {"Deployment", "NetworkPolicy", "Route", "Service"},
        "forbidden": {"ExternalSecret", "Ingress", "Secret"},
    },
    "migration": {
        "deployments": 0,
        "required": {"Job"},
        "forbidden": {
            "ConfigMap",
            "Deployment",
            "ExternalSecret",
            "Ingress",
            "Route",
            "Secret",
            "Service",
        },
    },
}

DEPRECATED_API_VERSIONS = {
    "extensions/v1beta1",
    "apps/v1beta1",
    "apps/v1beta2",
    "networking.k8s.io/v1beta1",
    "policy/v1beta1",
}


def fail(message: str) -> None:
    raise AssertionError(message)


def load_documents(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [doc for doc in yaml.safe_load_all(handle) if isinstance(doc, dict)]


def index_documents(
    documents: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for document in documents:
        kind = document.get("kind")
        name = document.get("metadata", {}).get("name")
        api_version = document.get("apiVersion")
        if not kind or not name or not api_version:
            fail(f"rendered object is missing apiVersion/kind/metadata.name: {document}")
        if api_version in DEPRECATED_API_VERSIONS:
            fail(f"{kind}/{name} uses removed API {api_version}")
        key = (kind, name)
        if key in indexed:
            fail(f"duplicate rendered object {kind}/{name}")
        indexed[key] = document
    return indexed


def validate_common(
    profile: str,
    documents: list[dict[str, Any]],
    indexed: dict[tuple[str, str], dict[str, Any]],
) -> None:
    contract = PROFILE_CONTRACTS[profile]
    counts = Counter(document["kind"] for document in documents)
    if counts["Deployment"] != contract["deployments"]:
        fail(
            f"{profile}: expected {contract['deployments']} Deployments, "
            f"rendered {counts['Deployment']}"
        )
    missing = contract["required"] - set(counts)
    if missing:
        fail(f"{profile}: missing required kinds {sorted(missing)}")
    present_forbidden = contract["forbidden"] & set(counts)
    if present_forbidden:
        fail(f"{profile}: rendered forbidden kinds {sorted(present_forbidden)}")

    for (kind, name), document in indexed.items():
        if kind != "Deployment":
            continue
        pod_spec = document["spec"]["template"]["spec"]
        if pod_spec.get("automountServiceAccountToken") is not False:
            fail(f"{profile}: Deployment/{name} must disable service account tokens")
        for container in pod_spec.get("containers", []):
            image = container.get("image", "")
            if not image or image.endswith(":latest"):
                fail(f"{profile}: Deployment/{name} has an unpinned image {image!r}")
            security = container.get("securityContext", {})
            if security.get("allowPrivilegeEscalation") is not False:
                fail(f"{profile}: Deployment/{name} permits privilege escalation")
            if security.get("readOnlyRootFilesystem") is not True:
                fail(f"{profile}: Deployment/{name} root filesystem is writable")
            if name.endswith("analytics"):
                secret_env_from = [
                    source
                    for source in container.get("envFrom", [])
                    if "secretRef" in source
                ]
                if secret_env_from:
                    fail(
                        f"{profile}: Deployment/{name} must not import a whole Secret"
                    )

    for document in documents:
        kind = document["kind"]
        name = document["metadata"]["name"]
        if kind == "ExternalSecret":
            if document["apiVersion"] != "external-secrets.io/v1":
                fail(
                    f"{profile}: ExternalSecret/{name} uses unsupported "
                    f"apiVersion {document['apiVersion']!r}"
                )
            spec = document.get("spec", {})
            if not spec.get("secretStoreRef", {}).get("name"):
                fail(f"{profile}: ExternalSecret/{name} has no SecretStore reference")
            if not spec.get("target", {}).get("name"):
                fail(f"{profile}: ExternalSecret/{name} has no target Secret name")
            if not spec.get("data"):
                fail(f"{profile}: ExternalSecret/{name} has no key mappings")
        elif kind == "Route":
            if document["apiVersion"] != "route.openshift.io/v1":
                fail(
                    f"{profile}: Route/{name} uses unsupported "
                    f"apiVersion {document['apiVersion']!r}"
                )
            spec = document.get("spec", {})
            if spec.get("to", {}).get("kind") != "Service":
                fail(f"{profile}: Route/{name} must target a Service")
            if spec.get("port", {}).get("targetPort") != "http":
                fail(f"{profile}: Route/{name} must target the named http port")
            if spec.get("tls", {}).get("termination") != "edge":
                fail(f"{profile}: Route/{name} must use supported edge TLS")
            timeout = document.get("metadata", {}).get("annotations", {}).get(
                "haproxy.router.openshift.io/timeout"
            )
            if timeout != "360s":
                fail(f"{profile}: Route/{name} must retain the 360s SSE timeout")
        elif kind in {"Gateway", "HTTPRoute"}:
            if document["apiVersion"] != "gateway.networking.k8s.io/v1":
                fail(
                    f"{profile}: {kind}/{name} uses unsupported "
                    f"apiVersion {document['apiVersion']!r}"
                )
        elif kind == "SecretProviderClass":
            if document["apiVersion"] != "secrets-store.csi.x-k8s.io/v1":
                fail(
                    f"{profile}: SecretProviderClass/{name} uses unsupported "
                    f"apiVersion {document['apiVersion']!r}"
                )


def require_resource(
    indexed: dict[tuple[str, str], dict[str, Any]], kind: str, name: str
) -> dict[str, Any]:
    try:
        return indexed[(kind, name)]
    except KeyError:
        fail(f"expected {kind}/{name} was not rendered")
        raise


def require_single_kind(
    documents: list[dict[str, Any]], profile: str, kind: str
) -> dict[str, Any]:
    matches = [document for document in documents if document["kind"] == kind]
    if len(matches) != 1:
        fail(f"{profile}: expected exactly one {kind}, rendered {len(matches)}")
    return matches[0]


def validate_aks_example(documents: list[dict[str, Any]]) -> None:
    profile = "aks-example"
    external_secret = require_single_kind(documents, profile, "ExternalSecret")
    store_ref = external_secret["spec"]["secretStoreRef"]
    if store_ref != {
        "name": "azure-key-vault",
        "kind": "ClusterSecretStore",
    }:
        fail(f"{profile}: unexpected ExternalSecret store reference {store_ref!r}")

    ingress = require_single_kind(documents, profile, "Ingress")
    if ingress["spec"].get("ingressClassName") != "nginx":
        fail(f"{profile}: UI Ingress must use the nginx class")
    annotations = ingress.get("metadata", {}).get("annotations", {})
    for key in (
        "nginx.ingress.kubernetes.io/proxy-read-timeout",
        "nginx.ingress.kubernetes.io/proxy-send-timeout",
    ):
        if annotations.get(key) != "360":
            fail(f"{profile}: Ingress annotation {key} must be '360'")

    service_accounts = [
        document for document in documents if document["kind"] == "ServiceAccount"
    ]
    if len(service_accounts) != 3:
        fail(
            f"{profile}: expected 3 Workload Identity ServiceAccounts, "
            f"rendered {len(service_accounts)}"
        )
    for service_account in service_accounts:
        name = service_account["metadata"]["name"]
        annotations = service_account["metadata"].get("annotations", {})
        if not annotations.get("azure.workload.identity/client-id"):
            fail(f"{profile}: ServiceAccount/{name} lacks Azure client-id annotation")

    for deployment in (
        document for document in documents if document["kind"] == "Deployment"
    ):
        name = deployment["metadata"]["name"]
        labels = deployment["spec"]["template"]["metadata"].get("labels", {})
        if labels.get("azure.workload.identity/use") != "true":
            fail(f"{profile}: Deployment/{name} lacks Workload Identity pod label")


def validate_eks_example(documents: list[dict[str, Any]]) -> None:
    profile = "eks-example"
    external_secret = require_single_kind(documents, profile, "ExternalSecret")
    store_ref = external_secret["spec"]["secretStoreRef"]
    if store_ref != {
        "name": "aws-secrets-manager",
        "kind": "ClusterSecretStore",
    }:
        fail(f"{profile}: unexpected ExternalSecret store reference {store_ref!r}")

    ingress = require_single_kind(documents, profile, "Ingress")
    if ingress["spec"].get("ingressClassName") != "alb":
        fail(f"{profile}: UI Ingress must use the ALB class")
    annotations = ingress.get("metadata", {}).get("annotations", {})
    attributes = annotations.get(
        "alb.ingress.kubernetes.io/load-balancer-attributes", ""
    )
    if "idle_timeout.timeout_seconds=360" not in attributes.split(","):
        fail(f"{profile}: ALB idle timeout must be 360 seconds for SSE")

    service_accounts = [
        document for document in documents if document["kind"] == "ServiceAccount"
    ]
    if len(service_accounts) != 3:
        fail(
            f"{profile}: expected 3 IRSA/Pod Identity ServiceAccounts, "
            f"rendered {len(service_accounts)}"
        )
    for service_account in service_accounts:
        name = service_account["metadata"]["name"]
        annotations = service_account["metadata"].get("annotations", {})
        if not annotations.get("eks.amazonaws.com/role-arn"):
            fail(f"{profile}: ServiceAccount/{name} lacks an IRSA role annotation")


def validate_defence(
    documents: list[dict[str, Any]],
    indexed: dict[tuple[str, str], dict[str, Any]],
    expected_path: Path,
) -> None:
    with expected_path.open(encoding="utf-8") as handle:
        expected = yaml.safe_load(handle)

    counts = Counter(document["kind"] for document in documents)
    for kind, count in expected["resourceCounts"].items():
        if counts[kind] != count:
            fail(f"defence: expected {count} {kind} objects, rendered {counts[kind]}")
    for kind in expected["forbiddenKinds"]:
        if counts[kind]:
            fail(f"defence: forbidden kind {kind} was rendered")

    for name, invariant in expected["deployments"].items():
        deployment = require_resource(indexed, "Deployment", name)
        spec = deployment["spec"]
        image = spec["template"]["spec"]["containers"][0]["image"]
        if image != invariant["image"]:
            fail(f"defence: Deployment/{name} image {image!r} != {invariant['image']!r}")
        if spec["strategy"]["type"] != invariant["strategy"]:
            fail(f"defence: Deployment/{name} must use {invariant['strategy']}")
        node_selector = spec["template"]["spec"].get("nodeSelector", {})
        if node_selector != invariant["nodeSelector"]:
            fail(
                f"defence: Deployment/{name} nodeSelector "
                f"{node_selector!r} != {invariant['nodeSelector']!r}"
            )

    for name, invariant in expected["services"].items():
        service = require_resource(indexed, "Service", name)
        service_type = service["spec"].get("type", "ClusterIP")
        if service_type != invariant["type"]:
            fail(f"defence: Service/{name} type {service_type!r} != {invariant['type']!r}")
        port = service["spec"]["ports"][0]["port"]
        if port != invariant["port"]:
            fail(f"defence: Service/{name} port {port!r} != {invariant['port']!r}")
        expected_annotations = invariant.get("annotations", {})
        annotations = service.get("metadata", {}).get("annotations", {})
        for key, value in expected_annotations.items():
            if annotations.get(key) != value:
                fail(f"defence: Service/{name} annotation {key} is not {value!r}")

    for name, expected_values in expected["configMaps"].items():
        config_map = require_resource(indexed, "ConfigMap", name)
        values = config_map.get("data", {})
        for key, value in expected_values.items():
            if values.get(key) != value:
                fail(
                    f"defence: ConfigMap/{name} value {key} "
                    f"{values.get(key)!r} != {value!r}"
                )

    gateway_expected = expected["gateway"]
    gateway = require_resource(indexed, "Gateway", gateway_expected["name"])
    gateway_spec = gateway["spec"]
    if gateway_spec.get("gatewayClassName") != gateway_expected["className"]:
        fail("defence: Gateway uses the wrong GatewayClass")
    internal_lb_annotations = {
        "service.beta.kubernetes.io/azure-load-balancer-internal": "true",
        "service.beta.kubernetes.io/azure-load-balancer-ipv4": gateway_expected[
            "privateIP"
        ],
    }
    for location, annotations in (
        ("metadata", gateway.get("metadata", {}).get("annotations", {})),
        (
            "infrastructure",
            gateway_spec.get("infrastructure", {}).get("annotations", {}),
        ),
    ):
        for key, value in internal_lb_annotations.items():
            if annotations.get(key) != value:
                fail(f"defence: Gateway {location} annotation {key} is not {value!r}")

    listeners = {
        listener["name"]: listener for listener in gateway_spec.get("listeners", [])
    }
    if set(listeners) != {"http", "https"}:
        fail("defence: Gateway must expose exactly the http and https listeners")
    for listener in listeners.values():
        if listener.get("hostname") != gateway_expected["hostname"]:
            fail("defence: Gateway listener hostname is incorrect")
        if listener.get("allowedRoutes", {}).get("namespaces", {}).get("from") != "Same":
            fail("defence: Gateway listeners must accept same-namespace routes only")
    if listeners["http"].get("port") != 80 or listeners["http"].get("protocol") != "HTTP":
        fail("defence: Gateway HTTP listener must use port 80")
    https_listener = listeners["https"]
    if https_listener.get("port") != 443 or https_listener.get("protocol") != "HTTPS":
        fail("defence: Gateway HTTPS listener must use port 443")
    certificate_refs = https_listener.get("tls", {}).get("certificateRefs", [])
    if len(certificate_refs) != 1 or certificate_refs[0].get("name") != gateway_expected[
        "tlsSecretName"
    ]:
        fail("defence: Gateway HTTPS listener references the wrong TLS Secret")

    routes_expected = expected["routes"]
    backend_route = require_resource(indexed, "HTTPRoute", routes_expected["backend"])
    backend_spec = backend_route["spec"]
    if backend_spec.get("hostnames") != [gateway_expected["hostname"]]:
        fail("defence: HTTPS HTTPRoute hostname is incorrect")
    backend_parent = backend_spec.get("parentRefs", [{}])[0]
    if (
        backend_parent.get("name") != gateway_expected["name"]
        or backend_parent.get("sectionName") != "https"
    ):
        fail("defence: HTTPS HTTPRoute must attach to the HTTPS listener")
    backend_ref = backend_spec.get("rules", [{}])[0].get("backendRefs", [{}])[0]
    if (
        backend_ref.get("name") != "jeen-insights-ui"
        or backend_ref.get("port") != 80
    ):
        fail("defence: HTTPS HTTPRoute must target Service/jeen-insights-ui:80")

    redirect_route = require_resource(indexed, "HTTPRoute", routes_expected["redirect"])
    redirect_spec = redirect_route["spec"]
    redirect_parent = redirect_spec.get("parentRefs", [{}])[0]
    if (
        redirect_parent.get("name") != gateway_expected["name"]
        or redirect_parent.get("sectionName") != "http"
    ):
        fail("defence: redirect HTTPRoute must attach to the HTTP listener")
    redirect = (
        redirect_spec.get("rules", [{}])[0]
        .get("filters", [{}])[0]
        .get("requestRedirect", {})
    )
    if redirect != {"scheme": "https", "statusCode": 301}:
        fail("defence: HTTPRoute must redirect HTTP to HTTPS with status 301")

    spc_expected = expected["secretProviderClass"]
    spc = require_resource(indexed, "SecretProviderClass", spc_expected["name"])
    spc_spec = spc["spec"]
    if spc_spec.get("provider") != spc_expected["provider"]:
        fail("defence: SecretProviderClass must use the Azure provider")
    parameters = spc_spec.get("parameters", {})
    for key, expected_key in (
        ("keyvaultName", "keyVaultName"),
        ("tenantId", "tenantId"),
        ("userAssignedIdentityID", "userAssignedIdentityID"),
    ):
        if parameters.get(key) != spc_expected[expected_key]:
            fail(f"defence: SecretProviderClass parameter {key} is incorrect")
    if parameters.get("usePodIdentity") != "false":
        fail("defence: SecretProviderClass must not use deprecated pod identity")
    if parameters.get("useVMManagedIdentity") != "true":
        fail("defence: SecretProviderClass must use VM managed identity")
    object_blocks = yaml.safe_load(parameters.get("objects", "")).get("array", [])
    objects = [yaml.safe_load(block) for block in object_blocks]
    object_names = {item.get("objectName") for item in objects}
    if object_names != {
        spc_expected["certificateObjectName"],
        spc_expected["keyObjectName"],
    } or any(item.get("objectType") != "secret" for item in objects):
        fail("defence: SecretProviderClass Key Vault object mappings are incorrect")
    secret_objects = spc_spec.get("secretObjects", [])
    if len(secret_objects) != 1:
        fail("defence: SecretProviderClass must sync exactly one TLS Secret")
    tls_secret = secret_objects[0]
    if (
        tls_secret.get("secretName") != spc_expected["tlsSecretName"]
        or tls_secret.get("type") != "kubernetes.io/tls"
    ):
        fail("defence: SecretProviderClass TLS Secret definition is incorrect")
    synced_keys = {
        item.get("objectName"): item.get("key") for item in tls_secret.get("data", [])
    }
    if synced_keys != {
        spc_expected["certificateObjectName"]: "tls.crt",
        spc_expected["keyObjectName"]: "tls.key",
    }:
        fail("defence: SecretProviderClass TLS key mappings are incorrect")

    ui_deployment = require_resource(indexed, "Deployment", "jeen-insights-ui")
    pod_spec = ui_deployment["spec"]["template"]["spec"]
    tls_volume = next(
        (volume for volume in pod_spec.get("volumes", []) if volume["name"] == "tls-secrets-store"),
        None,
    )
    if (
        tls_volume is None
        or tls_volume.get("csi", {}).get("driver") != "secrets-store.csi.k8s.io"
        or tls_volume.get("csi", {}).get("volumeAttributes", {}).get(
            "secretProviderClass"
        )
        != spc_expected["name"]
    ):
        fail("defence: UI Deployment does not mount the TLS SecretProviderClass")
    tls_mount = next(
        (
            mount
            for mount in pod_spec["containers"][0].get("volumeMounts", [])
            if mount["name"] == "tls-secrets-store"
        ),
        None,
    )
    if tls_mount is None or tls_mount.get("readOnly") is not True:
        fail("defence: UI TLS CSI volume must be mounted read-only")


def validate_digest(indexed: dict[tuple[str, str], dict[str, Any]]) -> None:
    deployments = [
        document
        for (kind, _), document in indexed.items()
        if kind == "Deployment"
    ]
    if len(deployments) != 3:
        fail(f"digest render: expected 3 Deployments, rendered {len(deployments)}")
    for deployment in deployments:
        name = deployment["metadata"]["name"]
        image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        if "@sha256:" not in image:
            fail(f"digest render: Deployment/{name} did not render repository@sha256")


def validate_migration(
    indexed: dict[tuple[str, str], dict[str, Any]],
) -> None:
    jobs = [
        document for (kind, _), document in indexed.items() if kind == "Job"
    ]
    if len(jobs) != 1:
        fail(f"migration: expected exactly one Job, rendered {len(jobs)}")
    job = jobs[0]
    name = job["metadata"]["name"]
    bundle_id = job.get("metadata", {}).get("labels", {}).get(
        "jeen.ai/migration-bundle"
    )
    if not bundle_id or not re.fullmatch(
        r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", bundle_id
    ):
        fail(f"migration: invalid DNS-safe bundle ID {bundle_id!r}")
    if name != f"jeen-insights-migrate-{bundle_id}":
        fail(f"migration: unexpected immutable Job name {name!r}")
    if "helm.sh/hook" in job.get("metadata", {}).get("annotations", {}):
        fail("migration: Job must never be a Helm hook")

    spec = job["spec"]
    if spec.get("backoffLimit") != 0:
        fail("migration: backoffLimit must default to zero")
    if "ttlSecondsAfterFinished" in spec:
        fail("migration: ttlSecondsAfterFinished must default to disabled")
    if not spec.get("activeDeadlineSeconds"):
        fail("migration: activeDeadlineSeconds must be bounded")

    pod_spec = spec["template"]["spec"]
    if pod_spec.get("automountServiceAccountToken") is not False:
        fail("migration: service account token must be disabled")
    container = pod_spec["containers"][0]
    if container.get("command") != ["python", "scripts/run_insights_migrations.py"]:
        fail("migration: Job does not run scripts/run_insights_migrations.py")
    security = container.get("securityContext", {})
    if security.get("allowPrivilegeEscalation") is not False:
        fail("migration: container permits privilege escalation")
    if security.get("readOnlyRootFilesystem") is not True:
        fail("migration: root filesystem is writable")
    env = {item["name"]: item.get("value") for item in container.get("env", [])}
    if env.get("ENCRYPT_MCP_TOKENS_BACKFILL") != "false":
        fail("migration: MCP token encryption backfill must default false")
    if not any("secretRef" in source for source in container.get("envFrom", [])):
        fail("migration: database Secret reference is missing")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=sorted(PROFILE_CONTRACTS))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected", type=Path)
    parser.add_argument("--digest", action="store_true")
    args = parser.parse_args()

    documents = load_documents(args.manifest)
    if not documents:
        fail(f"{args.manifest} contains no Kubernetes resources")
    indexed = index_documents(documents)

    if args.digest:
        validate_digest(indexed)
    else:
        if not args.profile:
            parser.error("--profile is required unless --digest is used")
        validate_common(args.profile, documents, indexed)
        if args.profile == "defence":
            if not args.expected:
                parser.error("--expected is required for the defence profile")
            validate_defence(documents, indexed, args.expected)
        elif args.profile == "aks-example":
            validate_aks_example(documents)
        elif args.profile == "eks-example":
            validate_eks_example(documents)
        elif args.profile == "migration":
            validate_migration(indexed)


if __name__ == "__main__":
    main()
