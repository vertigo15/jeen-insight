# Jeen Insights deployment

Use this directory as the operator entry point. The supported application
topology is the Helm umbrella chart in [`k8s_dev/`](k8s_dev/): private API and
analytics services plus one browser-facing UI service.

## Choose a platform

- [AKS direct Helm install](aks/INSTALL.md) — ACR, Azure Workload Identity,
  Key Vault/External Secrets, ingress or an internal load balancer. The
  defence cluster is a site-specific AKS overlay; use
  [`values.defence.yaml`](k8s_dev/values.defence.yaml) with its approved
  runbook instead of copying credentials into the generic guide.
- [EKS direct Helm install](eks/INSTALL.md) — ECR, IRSA or EKS Pod Identity,
  AWS Secrets Manager/External Secrets, ALB/NLB, ACM and Route 53.
- [OpenShift air-gap install](openshift/INSTALL.md) — offline image hand-off,
  internal registry, restricted SCC and Route.
- [Argo CD operator guide](argocd/README.md) — GitOps ownership, credential-free
  examples and the required explicit migration sync phase.

Before choosing values, read the canonical
[configuration inventory](configuration.md), [migration runbook](migrations.md)
and [OIDC setup](oidc.md).

## Common six-step installation

Every platform guide follows this same order. Do not install workload pods
before the migration Job succeeds.

### 1. Prepare registry

Build or receive the three `linux/amd64` images (`api`, `ui`, `analytics`),
scan them under local policy, and push immutable tags or digests to the target
registry. Grant the cluster pull access.

### 2. Configure values and secrets

Copy the platform example overlay, replace every `replace-with-*` placeholder,
set `PUBLIC_APP_URL`, and provision Kubernetes Secrets outside Helm. In a real
deployment keep `JEEN_DEV_MODE=false`, use separate internal API and analytics
secrets, and back up the deployment's `APP_ENCRYPTION_KEY`.

### 3. Preflight

Build chart dependencies, lint and render with the final overlay, validate the
render, confirm Secret/ExternalSecret readiness, database reachability, image
pull authorization, DNS/TLS and NetworkPolicy egress.

### 4. Run migration

Back up the shared metadata database. Render only
`templates/migration-job.yaml`, apply it, wait, and review its logs. See the
[migration runbook](migrations.md). The Job is disabled by default and is not
a Helm hook.

### 5. Install or upgrade

Only after migration success, run `helm upgrade --install` with the same
immutable image set and environment overlay. Keep
`RUN_MIGRATIONS_ON_START=false` and `SCHEMA_BOOTSTRAP_ON_START=false` for a
shared Schema Modeler database.

### 6. Verify

Wait for all rollouts, check the API baseline-verification log and all health
endpoints, then test login, one governed query and (when enabled) one analytics
request.

## Direct Helm versus Argo CD

Direct Helm means the operator owns dependency build, migration execution,
release values, Secret readiness, upgrades and rollback decisions. It is the
documented path for an initial or approved manual install.

With Argo CD, Git owns workload values and image references; avoid concurrent
manual `helm upgrade` of the same release. Schema migration remains a distinct,
observable phase that must complete before workloads sync. Argo must not turn
the migration into a workload-startup action or automatically reverse database
changes. See the [Argo CD operator guide](argocd/README.md).
