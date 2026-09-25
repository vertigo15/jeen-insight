# Jeen Insights Helm chart

This self-contained Helm umbrella chart deploys Jeen Insights as three
condition-gated components:

- `jeen-insights-api` — private FastAPI service on port 8000.
- `jeen-insights-ui` — Flask UI on port 8501 and the only ingress target.
- `jeen-insights-analytics` — isolated ML-skills sandbox on port 8100.

The layout follows the Jeen platform convention: stable base values plus an
environment overlay, immutable image tags, externally managed secrets, and a
GitOps handoff for long-lived environments.

## Values layers

`values.yaml` is environment-agnostic and uses explicit `registry.invalid`
image placeholders so linting and rendering need no private registry. It has
no public hostname. `values.aks-dev.yaml` supplies the AKS
development registry, node selector, ingress host/TLS secret, resource values,
and browser-facing `PUBLIC_APP_URL`.

Credential-free starting profiles are `values.aks.example.yaml` and
`values.eks.example.yaml`; replace every documented placeholder before use.
For air-gapped OpenShift, the canonical overlay is
`deployment/k8s_dev/values.openshift.yaml`
and the delivery flow (image tarballs, Secret, Route) is documented in
[deployment/openshift/README.md](../openshift/README.md).

Use both files, with the environment overlay last:

```sh
helm dependency build deployment/k8s_dev
helm lint --with-subcharts deployment/k8s_dev \
  --values deployment/k8s_dev/values.aks-dev.yaml

helm template jeen-insights deployment/k8s_dev \
  --namespace jeen-data \
  --values deployment/k8s_dev/values.aks-dev.yaml \
  > /tmp/jeen-insights.yaml
```

`Chart.lock` pins the local API/UI/analytics dependency metadata. The generated
`charts/*.tgz` archives are intentionally ignored and are rebuilt by
`helm dependency build`.

Each component accepts `image.repository` plus `image.tag` or
`image.digest`. A non-empty digest takes precedence and renders
`repository@digest`; existing `--set ...image.tag=...` deployment commands
remain supported.

## Secrets

No Helm template creates a plaintext `Secret`. The default path references an
existing `jeen-insights-secrets` Secret. It must contain the populated values
required by `.env.example`; do not use template placeholder values.

Hardened mode is enabled by default. Provide strong `FLASK_SECRET_KEY` and
`APP_ENCRYPTION_KEY` values, plus the metadata database and Azure OpenAI
credentials. The components do not start correctly with the placeholder
security values from `.env.example`.

For AKS environments with External Secrets Operator, set
`global.externalSecrets.enabled=true`, configure the existing
`ClusterSecretStore`, and map Kubernetes keys to Key Vault keys:

```yaml
global:
  externalSecrets:
    enabled: true
    secretStoreRef:
      name: azure-kv-store
      kind: ClusterSecretStore
    secretMappings:
      METADATA_DB_PASSWORD: METADATA-DB-PASSWORD
      AZURE_OPENAI_API_KEY: AZURE-OPENAI-API-KEY
```

`creationPolicy` defaults to `Owner`, which makes External Secrets the sole
author of the target Secret and prunes any key not listed in `secretMappings`.
Use `Merge` to have Key Vault own only the mapped keys while the rest of a
hand-managed Secret is left intact.

Wait for the generated Secret to be present and the `ExternalSecret` to report
`SecretSynced` before starting the Helm release.

Each component can override `existingSecret.name` and
`existingSecret.optional`; blank/null fields inherit
`global.existingSecret`. This supports split, least-privilege Secrets without
breaking existing values files. The analytics pod still projects only the
`INTERNAL_ANALYTICS_SECRET` key and never imports an entire Secret with
`envFrom`.

Secret data cannot be safely hashed when it is authored outside Helm. After
rotating a referenced Secret, bump `global.secretVersion`; that explicit value
is copied into every workload pod-template annotation and triggers a rollout.

### APP_ENCRYPTION_KEY must be identical everywhere sharing a metadata DB

`APP_ENCRYPTION_KEY` is the key-encryption key for connector secrets: per-user
OAuth grants, refresh tokens, and connector client secrets. It is one key per
deployment, not per user, and it never expires.

Every environment pointed at the same metadata database **must** use the same
value. Two deployments sharing one database with different keys cannot read each
other's stored grants, and the failure is silent in a way that looks like expired
consent: the user is asked to reconnect, reconnecting rewrites the grant under
one key, and the other environment breaks in turn. In the dev environment the
canonical value therefore lives in Key Vault as
`JEEN-INSIGHTS-APP-ENCRYPTION-KEY` (`jeen-kv-aks-dev`), synced by the
`Merge`-policy `ExternalSecret` in `values.aks-dev.yaml`.

Local development must hold that same value in `.env`. Note that the vault's data
plane resolves to a private endpoint, so `az keyvault secret show` does not work
from a workstation outside the cluster network; retrieve the value from a host
inside the VNet, or ask an operator for it. Rotating the key requires
re-encrypting stored connector secrets, which in practice means re-creating the
connector client secrets and having every user reconnect.

## Ingress and runtime requirements

The AKS-dev overlay uses HTTPS at `jeen-insights.dev.jeenai.app`, configures
`PUBLIC_APP_URL` for Entra redirect construction, and sets
`SESSION_COOKIE_SECURE=true`. It references the existing `cloudflare-tls`
Secret; provision that Secret through the cluster's approved TLS automation
before exposing the ingress.

The ingress disables response buffering and raises proxy read/send timeouts for
the UI's streaming insight responses.

## Optional platform controls

- HPA and PDB rendering is disabled by default. Enable a PDB only when the
  associated workload has enough replicas to satisfy `minAvailable`.
- NetworkPolicy is disabled by default. When enabled, configure ingress
  controller namespaces and explicit approved CIDRs for PostgreSQL/HTTPS
  egress. DNS and UI-to-API traffic are rendered automatically.
- Both components use non-root security contexts, dropped Linux capabilities,
  a read-only root filesystem, bounded `/tmp`, and startup/liveness/readiness
  probes.

## Schema migrations on a shared metadata DB

All shared-DB overlays set `RUN_MIGRATIONS_ON_START=false` and
`SCHEMA_BOOTSTRAP_ON_START=false`. Insights revisions
(`db/migrations/insights/*.sql`) are applied once by the chart's
values-driven migration Job, never by a workload pod. The Job is disabled by
default and is deliberately not a Helm hook.

Copy `values.migration.example.yaml`, replace its placeholders, and render it
with the target environment overlay. The example disables API, UI, analytics,
and ExternalSecret so the output is migration-only:

```sh
BUNDLE_ID=replace-with-dns-safe-immutable-release-id
API_REPOSITORY=registry.example.invalid/jeen-insights-api
API_TAG=replace-with-immutable-api-tag
MIGRATION_SECRET=replace-with-migration-secret
KUBE_CONTEXT=replace-with-context
NAMESPACE=replace-with-namespace
helm dependency build deployment/k8s_dev
helm template jeen-insights deployment/k8s_dev \
  --namespace "$NAMESPACE" \
  --values deployment/k8s_dev/values.aks.example.yaml \
  --values deployment/k8s_dev/values.migration.example.yaml \
  --set-string migration.bundleId="$BUNDLE_ID" \
  --set-string migration.image.repository="$API_REPOSITORY" \
  --set-string migration.image.tag="$API_TAG" \
  --set-string migration.existingSecret.name="$MIGRATION_SECRET" \
  --show-only templates/migration-job.yaml \
  > /tmp/jeen-insights-migration.yaml

kubectl --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" \
  apply -f /tmp/jeen-insights-migration.yaml
kubectl --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" \
  wait --for=condition=complete \
  job/jeen-insights-migrate-"$BUNDLE_ID" --timeout=15m
kubectl --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" \
  logs job/jeen-insights-migrate-"$BUNDLE_ID"
```

Only after the Job completes and its log is reviewed should the workload
`helm upgrade --install` run. A non-empty image digest takes precedence over
the tag. `migration.apiConfigMap` is optional so first install can migrate
before workload ConfigMaps exist; the Secret reference defaults to
`global.existingSecret` but can point to a dedicated migration Secret.

The runner bounds its own waits (`MIGRATION_LOCK_WAIT_SECONDS`,
`MIGRATION_LOCK_TIMEOUT`, `MIGRATION_STATEMENT_TIMEOUT`, all validated as
positive and finite) so it fails fast instead of queuing behind another
session, pins `search_path` to `public`, and only ever creates or
alters Insights-owned objects (`insights_*`, `connector*`, `auth_users`,
`app_settings`).

Use a dedicated migration DB role where possible: grant only the DDL/DML needed
for Insights-owned objects and keep its credentials out of workload Secrets.
Take and verify a database backup before each migration. Migrations are
forward-only and should follow expand/contract sequencing so old and new
application versions can overlap safely. Rolling back a Helm release or image
does not undo schema changes; an incompatible schema rollback requires a
tested restore or an explicit forward repair.

With bootstrap disabled, API pods issue no DDL: at boot they verify the
baseline tables and columns and refuse to start clearly when the Job has not
run. Prompt seeding at boot stays (rows in Insights' own
`insights_prompts`).

## Validate and deploy

The chart supports Kubernetes 1.24 and newer, including OpenShift 4.11. Run
the pinned local validation entry point before deploying:

```sh
deployment/tests/validate-helm.sh
```

It requires Helm 3.18.4 and renders base, generic AKS, generic EKS, AKS dev,
defence, canonical OpenShift, and migration-only profiles against Kubernetes
1.24 capabilities. It also checks platform semantics, digest rendering, and
defence invariants, and uses kubeconform 0.6.7 when available. CI requires
kubeconform; local runs can omit it or set `REQUIRE_KUBECONFORM=1` to enforce
its presence.

Override component images with the immutable tag built for the release:

```sh
RELEASE_TAG=replace-with-immutable-release-tag
helm upgrade --install jeen-insights deployment/k8s_dev \
  --namespace jeen-data \
  --values deployment/k8s_dev/values.aks-dev.yaml \
  --set-string jeen-insights-api.image.tag="$RELEASE_TAG" \
  --set-string jeen-insights-ui.image.tag="$RELEASE_TAG" \
  --set-string jeen-insights-analytics.image.tag="$RELEASE_TAG" \
  --atomic --wait --timeout 10m

kubectl --context aks-jeen-dev-weu-001 --namespace jeen-data \
  apply --dry-run=server -f /tmp/jeen-insights.yaml
```

`jeen-data` is GitOps-managed. For a durable deployment, have the platform
team add this chart and the AKS overlay to the namespace's Argo CD source of
truth; use direct Helm only as the approved interim delivery path.

## CI/CD

Pushes to `main` run `.github/workflows/deploy.yml`. After tests pass, the
workflow builds immutable API, UI, and analytics images, pushes them to ACR, and upgrades
the `jeen-insights` Helm release in the `jeen-data` namespace on
`aks-jeen-dev-weu-001`. The deployment waits for the ExternalSecret, performs
an atomic Helm upgrade, verifies both rollout images, and checks the public
health endpoint.

The `github-actions-jeen-insights` service principal requires cluster-user
access on the AKS resource and namespace-scoped AKS RBAC Admin access on
`jeen-data`. Namespace admin is required because the chart manages an
`ExternalSecret` custom resource.
