# Install Jeen Insights on EKS

This is the generic direct-Helm procedure for EKS. Read
[configuration](../configuration.md), [migrations](../migrations.md), and
[OIDC](../oidc.md) before creating the environment overlay.

## Prerequisites

- EKS with Linux `amd64` nodes, Kubernetes 1.24+, AWS CLI, `kubectl`, Docker and
  Helm 3.18.4.
- ECR repositories and node/Pod pull permission.
- PostgreSQL initialized by Jeen Schema Modeler; typically private RDS
  PostgreSQL reachable from pod subnets/security groups.
- AWS Load Balancer Controller for ALB ingress (or an approved NLB service),
  ACM certificate, and Route 53/private DNS.
- External Secrets Operator (ESO) and AWS Secrets Manager, authenticated by
  IRSA or EKS Pod Identity.
- NetworkPolicy enforcement enabled in the selected CNI before relying on
  chart policies.

## 1. Prepare registry

```sh
export AWS_REGION=replace-with-region
export AWS_ACCOUNT_ID=replace-with-account-id
export EKS_CLUSTER=replace-with-eks-cluster
export NAMESPACE=jeen-insights
export RELEASE=jeen-insights
export IMAGE_TAG=release-20260925-a1b2c3d
export ECR_REGISTRY="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

aws eks update-kubeconfig --region "$AWS_REGION" --name "$EKS_CLUSTER"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"

for component in api ui analytics; do
  repository="jeen-insights-${component}"
  aws ecr describe-repositories --region "$AWS_REGION" \
    --repository-names "$repository" >/dev/null 2>&1 \
    || aws ecr create-repository --region "$AWS_REGION" \
      --repository-name "$repository" >/dev/null
  docker tag "$repository:$IMAGE_TAG" \
    "$ECR_REGISTRY/$repository:$IMAGE_TAG"
  docker push "$ECR_REGISTRY/$repository:$IMAGE_TAG"
done
```

Use immutable ECR tags and enable the repository scan/lifecycle controls
required by local policy.

## 2. Configure values and secrets

```sh
cp deployment/k8s_dev/values.eks.example.yaml \
  /tmp/jeen-insights-eks.yaml
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
```

Replace every `replace-with-*` entry. Set repositories/tags,
`PUBLIC_APP_URL`, ALB certificate annotation, approved VPC/RDS egress CIDRs,
node policy and resource limits.

For an internet-facing or internal ALB, set
`alb.ingress.kubernetes.io/scheme`, ACM
`alb.ingress.kubernetes.io/certificate-arn`, target type `ip`, health path and
360-second idle timeout. After creation, point a Route 53 alias (or
ExternalDNS) at the ALB. For a TCP NLB instead, disable ingress, set the UI
Service to `LoadBalancer`, use the AWS NLB annotations, and terminate TLS in an
approved layer; keep `PUBLIC_APP_URL` on HTTPS.

Create a least-privilege identity for ESO that may read only the deployment's
Secrets Manager paths. With IRSA, annotate ESO's ServiceAccount with the role
ARN and configure the store to use that JWT identity. With EKS Pod Identity,
remove the IRSA annotation and create a Pod Identity association for the ESO
ServiceAccount. Application ServiceAccount roles are separate and normally do
not need Secrets Manager access because ESO materializes the Kubernetes Secret.

Seed AWS Secrets Manager out of band and map remote names in the overlay. Do
not place secret values in Git or shell history. Prefer component-specific
Secrets: analytics receives only `INTERNAL_ANALYTICS_SECRET`; migration uses a
dedicated DB role; UI and API receive only their own keys. Back up the shared
database KEK (`APP_ENCRYPTION_KEY`).

RDS requires SSL, but `METADATA_DB_SSL=true` currently maps to
`sslmode=require` only. The application does not expose the RDS CA bundle or
`verify-full` hostname verification. Keep RDS private, enforce TLS server-side,
record this limitation in the risk/control register, and do not describe the
client connection as certificate-verified.

## 3. Preflight

```sh
helm dependency build deployment/k8s_dev
helm lint --with-subcharts deployment/k8s_dev \
  --values /tmp/jeen-insights-eks.yaml
helm template "$RELEASE" deployment/k8s_dev \
  --namespace "$NAMESPACE" \
  --values /tmp/jeen-insights-eks.yaml \
  > /tmp/jeen-insights-eks-rendered.yaml
kubectl --namespace "$NAMESPACE" apply --dry-run=server \
  -f /tmp/jeen-insights-eks-rendered.yaml
kubectl --namespace "$NAMESPACE" wait --for=condition=Ready \
  externalsecret/jeen-insights-secrets --timeout=5m
kubectl --namespace "$NAMESPACE" get secret jeen-insights-secrets
```

Run `deployment/tests/validate-helm.sh` with its pinned tools. Confirm ECR pull
authorization, RDS security-group/NACL/DNS paths, Secrets Manager/STS egress,
IdP/provider egress, ALB Controller readiness, ACM certificate region, and
Route 53 ownership.

NetworkPolicy does not replace security groups or NACLs. Enable CNI policy
enforcement first, then explicitly allow DNS, API-to-analytics, UI-to-API,
RDS TCP/5432 and approved HTTPS destinations. Keep analytics egress denied.

## 4. Run migration

```sh
export BUNDLE_ID=release-20260925-a1b2c3d
export MIGRATION_SECRET=jeen-insights-migration

helm template "$RELEASE" deployment/k8s_dev \
  --namespace "$NAMESPACE" \
  --values /tmp/jeen-insights-eks.yaml \
  --values deployment/k8s_dev/values.migration.example.yaml \
  --set-string migration.bundleId="$BUNDLE_ID" \
  --set-string migration.image.repository="$ECR_REGISTRY/jeen-insights-api" \
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

Stop on failure and follow the [migration recovery policy](../migrations.md).

## 5. Install or upgrade

```sh
helm upgrade --install "$RELEASE" deployment/k8s_dev \
  --namespace "$NAMESPACE" --create-namespace \
  --values /tmp/jeen-insights-eks.yaml \
  --set-string jeen-insights-api.image.tag="$IMAGE_TAG" \
  --set-string jeen-insights-ui.image.tag="$IMAGE_TAG" \
  --set-string jeen-insights-analytics.image.tag="$IMAGE_TAG" \
  --rollback-on-failure --wait --timeout 10m
```

For upgrades, push new immutable images, preflight, back up RDS, run a new
migration bundle, and then upgrade. Bump `global.secretVersion` after secret
rotation.

Set `HELM_REVISION=replace-with-helm-revision`, then
`helm rollback "$RELEASE" "$HELM_REVISION" --wait`. This affects workloads
only. Confirm compatibility with the forward schema first; no command in this
flow automatically reverses database migrations.

## 6. Verify

```sh
kubectl --namespace "$NAMESPACE" rollout status \
  deployment/jeen-insights-api deployment/jeen-insights-ui \
  deployment/jeen-insights-analytics --timeout=10m
kubectl --namespace "$NAMESPACE" logs deployment/jeen-insights-api \
  | grep -i "baseline verified"
kubectl --namespace "$NAMESPACE" get ingress,service
curl --fail --show-error --silent \
  "https://insights.example.invalid/health"
```

Test login, a governed query and one analytics request when enabled.

Troubleshooting:

- `ImagePullBackOff`: verify ECR region/account/tag and node/Pod IAM policy.
- ESO not Ready: inspect the store and ServiceAccount; check IRSA trust
  conditions or the Pod Identity association plus `secretsmanager:GetSecretValue`.
- Migration/database timeout: check RDS security groups, pod subnets, DNS,
  NACLs and chart NetworkPolicy.
- ALB absent/unhealthy: inspect controller logs, subnet tags, ACM ARN/region,
  target health and `/health`.
- Redirect mismatch: align Route 53 name, ALB HTTPS listener,
  `PUBLIC_APP_URL` and IdP callback.
- Streaming disconnects: keep ALB idle timeout at 360 seconds.
