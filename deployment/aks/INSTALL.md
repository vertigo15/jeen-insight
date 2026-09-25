# Install Jeen Insights on AKS

This is the generic direct-Helm procedure. The defence environment has a
site-specific overlay at
[`../k8s_dev/values.defence.yaml`](../k8s_dev/values.defence.yaml); use its
approved runbook and never copy defence identifiers or secrets into this guide.

## Prerequisites

- AKS with Linux `amd64` user nodes, Kubernetes 1.24+, `kubectl`, Azure CLI and
  Helm 3.18.4.
- ACR and kubelet pull access (`AcrPull`) or an approved image pull Secret.
- PostgreSQL initialized by Jeen Schema Modeler and reachable on TCP 5432.
- Ingress controller plus TLS automation, or an approved internal
  `LoadBalancer` and private DNS.
- External Secrets Operator (ESO), Azure Key Vault, and AKS Workload Identity
  when using the recommended secret path.

Read [configuration](../configuration.md),
[migrations](../migrations.md), and [OIDC](../oidc.md) first.

## 1. Prepare registry

Set non-secret shell identifiers, create repositories by pushing the immutable
release images, and attach ACR to AKS:

```sh
export AZURE_SUBSCRIPTION=replace-with-subscription
export RESOURCE_GROUP=replace-with-resource-group
export AKS_CLUSTER=replace-with-aks-cluster
export ACR_NAME=replacewithacrname
export NAMESPACE=jeen-insights
export RELEASE=jeen-insights
export IMAGE_TAG=release-20260925-a1b2c3d

az account set --subscription "$AZURE_SUBSCRIPTION"
az acr login --name "$ACR_NAME"
az aks update --resource-group "$RESOURCE_GROUP" --name "$AKS_CLUSTER" \
  --attach-acr "$ACR_NAME"
az aks get-credentials --resource-group "$RESOURCE_GROUP" \
  --name "$AKS_CLUSTER" --overwrite-existing

export ACR_LOGIN_SERVER="$(az acr show --name "$ACR_NAME" \
  --query loginServer --output tsv)"
docker tag jeen-insights-api:"$IMAGE_TAG" \
  "$ACR_LOGIN_SERVER/jeen-insights-api:$IMAGE_TAG"
docker tag jeen-insights-ui:"$IMAGE_TAG" \
  "$ACR_LOGIN_SERVER/jeen-insights-ui:$IMAGE_TAG"
docker tag jeen-insights-analytics:"$IMAGE_TAG" \
  "$ACR_LOGIN_SERVER/jeen-insights-analytics:$IMAGE_TAG"
docker push "$ACR_LOGIN_SERVER/jeen-insights-api:$IMAGE_TAG"
docker push "$ACR_LOGIN_SERVER/jeen-insights-ui:$IMAGE_TAG"
docker push "$ACR_LOGIN_SERVER/jeen-insights-analytics:$IMAGE_TAG"
```

Use an immutable digest where policy requires it.

## 2. Configure values and secrets

```sh
cp deployment/k8s_dev/values.aks.example.yaml \
  /tmp/jeen-insights-aks.yaml
```

Replace every `replace-with-*` value. Set all three repositories/tags,
`PUBLIC_APP_URL`, ingress host/TLS Secret, approved NetworkPolicy CIDRs, and
resource/node-pool policy. For internal-only access, disable ingress and use
the commented internal Azure Load Balancer service settings with private DNS.

Enable Workload Identity if not already enabled:

```sh
az aks update --resource-group "$RESOURCE_GROUP" --name "$AKS_CLUSTER" \
  --enable-oidc-issuer --enable-workload-identity
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
```

Create a user-assigned managed identity and its federated credential using your
platform IaC. Grant it only `Key Vault Secrets User` on the deployment vault.
Configure ESO's `SecretStore`/`ClusterSecretStore` for Azure Key Vault with
that identity, then set its name and the remote secret mappings in the
overlay. Seed the vault out of band; do not put values on a command line or in
Git.

The recommended boundaries are separate UI, API, analytics and migration
Secrets. The example uses one ESO target for portability; if splitting, set
each component's `existingSecret.name`. Back up `APP_ENCRYPTION_KEY` and keep
`INTERNAL_ANALYTICS_SECRET` distinct from `INTERNAL_API_SECRET`.

For OIDC private CAs, create the CA ConfigMap separately and add UI
`extraVolumes`/`extraVolumeMounts`; see [OIDC](../oidc.md).

## 3. Preflight

```sh
helm dependency build deployment/k8s_dev
helm lint --with-subcharts deployment/k8s_dev \
  --values /tmp/jeen-insights-aks.yaml
helm template "$RELEASE" deployment/k8s_dev \
  --namespace "$NAMESPACE" \
  --values /tmp/jeen-insights-aks.yaml \
  > /tmp/jeen-insights-aks-rendered.yaml
kubectl --namespace "$NAMESPACE" apply --dry-run=server \
  -f /tmp/jeen-insights-aks-rendered.yaml
kubectl --namespace "$NAMESPACE" wait --for=condition=Ready \
  externalsecret/jeen-insights-secrets --timeout=5m
kubectl --namespace "$NAMESPACE" get secret jeen-insights-secrets
```

Run `deployment/tests/validate-helm.sh` when the pinned Helm/kubeconform tools
are installed. Confirm ACR pulls, PostgreSQL/private DNS reachability, ingress
class/TLS Secret, and allowed egress to PostgreSQL, Key Vault, IdPs and external
providers.

## 4. Run migration

Use a dedicated migration Secret when available:

```sh
export BUNDLE_ID=release-20260925-a1b2c3d
export MIGRATION_SECRET=jeen-insights-migration

helm template "$RELEASE" deployment/k8s_dev \
  --namespace "$NAMESPACE" \
  --values /tmp/jeen-insights-aks.yaml \
  --values deployment/k8s_dev/values.migration.example.yaml \
  --set-string migration.bundleId="$BUNDLE_ID" \
  --set-string migration.image.repository="$ACR_LOGIN_SERVER/jeen-insights-api" \
  --set-string migration.image.tag="$IMAGE_TAG" \
  --set-string migration.existingSecret.name="$MIGRATION_SECRET" \
  --show-only templates/migration-job.yaml \
  > /tmp/jeen-insights-migration.yaml
kubectl --namespace "$NAMESPACE" apply -f /tmp/jeen-insights-migration.yaml
kubectl --namespace "$NAMESPACE" wait --for=condition=complete \
  "job/${RELEASE}-migrate-${BUNDLE_ID}" --timeout=15m
kubectl --namespace "$NAMESPACE" logs \
  "job/${RELEASE}-migrate-${BUNDLE_ID}"
```

Stop if the Job or log review fails. Follow the full
[migration policy](../migrations.md).

## 5. Install or upgrade

```sh
helm upgrade --install "$RELEASE" deployment/k8s_dev \
  --namespace "$NAMESPACE" --create-namespace \
  --values /tmp/jeen-insights-aks.yaml \
  --set-string jeen-insights-api.image.tag="$IMAGE_TAG" \
  --set-string jeen-insights-ui.image.tag="$IMAGE_TAG" \
  --set-string jeen-insights-analytics.image.tag="$IMAGE_TAG" \
  --rollback-on-failure --wait --timeout 10m
```

For every later release: push new immutable images, preflight, back up, run a
new migration bundle, then repeat `helm upgrade`. After external Secret
rotation, bump `global.secretVersion` to roll pods.

Set `HELM_REVISION=replace-with-helm-revision`, then
`helm rollback "$RELEASE" "$HELM_REVISION" --wait`. This rolls back workloads
only. Confirm the old code is compatible with the forward schema first;
database migrations are never automatically rolled back.

## 6. Verify

```sh
kubectl --namespace "$NAMESPACE" rollout status \
  deployment/jeen-insights-api deployment/jeen-insights-ui \
  deployment/jeen-insights-analytics --timeout=10m
kubectl --namespace "$NAMESPACE" logs deployment/jeen-insights-api \
  | rg -i "baseline verified"
curl --fail --show-error --silent \
  "https://insights.example.invalid/health"
```

Test first-admin setup or OIDC login, one governed query, and one ML request
when enabled.

Troubleshooting:

- `ImagePullBackOff`: verify ACR repository/tag and kubelet `AcrPull`.
- ESO not Ready: inspect `kubectl describe externalsecret` and the
  `SecretStore`; verify federated subject and Key Vault RBAC.
- API baseline failure: migration did not complete against this database.
- PostgreSQL timeout: check private DNS, VNet routing, firewall and chart
  NetworkPolicy CIDRs.
- Redirect mismatch: make ingress host, `PUBLIC_APP_URL`, TLS scheme and IdP
  callback identical.
- Streaming disconnects: retain ingress proxy buffering off and long
  read/send timeouts.
