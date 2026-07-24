# Jeen Insights — AKS development chart

This self-contained Helm umbrella chart deploys Jeen Insights as two
condition-gated components:

- `jeen-insights-api` — private FastAPI service on port 8000.
- `jeen-insights-ui` — Flask UI on port 8501 and the only ingress target.

The layout follows the Jeen platform convention: stable base values plus an
environment overlay, immutable image tags, externally managed secrets, and a
GitOps handoff for long-lived environments.

## Values layers

`values.yaml` is environment-agnostic and intentionally has no deployable
image references or public hostname. `values.aks-dev.yaml` supplies the AKS
development registry, node selector, ingress host/TLS secret, resource values,
and browser-facing `PUBLIC_APP_URL`.

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

`Chart.lock` pins the local API/UI dependency metadata. The generated
`charts/*.tgz` archives are intentionally ignored and are rebuilt by
`helm dependency build`.

## Secrets

No Helm template creates a plaintext `Secret`. The default path references an
existing `jeen-insights-secrets` Secret. It must contain the populated values
required by `.env.example`; do not use template placeholder values.

Hardened mode is enabled by default. Provide strong `FLASK_SECRET_KEY` and
`APP_ENCRYPTION_KEY` values, plus the metadata database and Azure OpenAI
credentials. The API and UI do not start correctly with the placeholder
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

Wait for the generated Secret to be present and the `ExternalSecret` to report
`SecretSynced` before starting the Helm release.

## Ingress and runtime requirements

The AKS-dev overlay uses HTTPS at `jeen-insights.dev.jeenai.app`, configures
`PUBLIC_APP_URL` for Entra redirect construction, and sets
`SESSION_COOKIE_SECURE=true`. Provision the `jeen-insights-dev-tls` certificate
through the cluster's approved TLS automation before exposing the ingress.

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

## Validate and deploy

Override both images with the immutable tag built for the release:

```sh
helm upgrade --install jeen-insights deployment/k8s_dev \
  --namespace jeen-data \
  --values deployment/k8s_dev/values.aks-dev.yaml \
  --set jeen-insights-api.image.tag=<release-tag> \
  --set jeen-insights-ui.image.tag=<release-tag> \
  --atomic --wait --timeout 10m

kubectl --context aks-jeen-dev-weu-001 --namespace jeen-data \
  apply --dry-run=server -f /tmp/jeen-insights.yaml
```

`jeen-data` is GitOps-managed. For a durable deployment, have the platform
team add this chart and the AKS overlay to the namespace's Argo CD source of
truth; use direct Helm only as the approved interim delivery path.
