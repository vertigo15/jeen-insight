# Argo CD operator guide

This is the canonical guide for operating Jeen Insights with Argo CD. The
examples in this directory are credential-free templates: every repository,
cluster, registry, revision, and release identifier is an
`example.invalid`/example placeholder that must be replaced by reviewed Git
changes.

## Ownership boundary

One environment has exactly one release owner. If Argo CD owns an environment,
no person, pipeline, or direct-Helm CI job may run `helm install`, `helm
upgrade`, `helm rollback`, or `helm uninstall` for that release. CI may lint
and render the chart, but it cannot co-own or mutate an Argo-managed
environment. Conversely, do not point these Applications at a release still
owned by direct Helm.

Git owns the AppProject, Applications, chart revision, values, immutable image
references, migration bundle ID, and sync policy. Argo owns only the resources
rendered from those inputs. A secret manager or a cluster operator owns secret
material.

## Prerequisites

- Argo CD is installed and healthy; the examples use the `argocd` namespace.
- The deployment repository is registered read-only in Argo CD. Put repository
  credentials in Argo's credential mechanism, never in these manifests.
- The target cluster is registered in Argo CD. Replace
  `https://kubernetes.example.invalid` with its registered server value.
- Images are built for the target architecture, scanned, and pushed under
  immutable tags or digests.
- The target database is backed up and the migration prerequisites in the
  [migration runbook](../migrations.md) are satisfied.
- `jeen-insights-secrets` and, when custom IdP trust is needed,
  `jeen-insights-idp-ca` already exist under the ownership model below.
- If External Secrets Operator (ESO) is used, its controller and CRDs are
  installed and healthy before the secret-bootstrap Application is synced.

Register the repository and cluster with the normal Argo CD administrator
workflow, then replace all example URLs/revisions in one reviewed pull request.
Do not commit repository, cluster, cloud, database, or secret credentials.

## AppProject and least-practical access

Apply [`project.yaml`](project.yaml) through the bootstrap Application or by an
Argo administrator. It allows only the exact example repository, cluster, and
namespace. Its resource allowlists cover the current chart, namespace creation,
and namespaced ESO objects; it does not permit Argo to create Kubernetes
`Secret` objects.

Copy and narrow the project per real environment. Add a resource kind only
after a reviewed chart change needs it. Do not change the destination or source
to `*`, and do not add credential-bearing repository or cluster Secrets to Git.

## Bootstrap and phase order

[`bootstrap-application.yaml`](bootstrap-application.yaml) is the app-of-apps
entry point. It renders [`kustomization.yaml`](kustomization.yaml), which
creates the following child Application resources in sync-wave order:

1. `-3`: AppProject.
2. `-2`: operator-owned namespace, namespaced SecretStore, and ExternalSecret
   references. A cluster-scoped store must be pre-created by the platform team.
3. `-1`: a migration-only Application.
4. `0`: the long-lived workload Application.

These waves order creation/update of the child `Application` objects only.
They do **not** prove that a child Application has synced or that its resources
are healthy. Argo CD no longer supplies a built-in health assessment for the
`argoproj.io/Application` CRD. A platform may add a reviewed
`resource.customizations.health.argoproj.io_Application` assessment that
propagates the child's `status.health`, but this repository does not assume that
cluster-wide customization exists. The concrete migration and workload
Applications therefore both require manual sync; workload promotion is the
operator's explicit post-migration gate.

The child Applications use `CreateNamespace=true`,
`PruneLast=true`, and `FailOnSharedResource=true`. `CreateNamespace` creates
only an empty Namespace; it does not create secret values. `PruneLast` delays
pruning until other resources are healthy. `FailOnSharedResource` stops the
sync if another Application already owns a resource, enforcing the single-owner
boundary.

The secret-bootstrap Application references operator-owned manifests; this
repository deliberately contains no Secret payload. For ESO, install and
establish the CRDs/controller first, then a `SecretStore` or
`ClusterSecretStore`, then its `ExternalSecret`. Wait for the resulting Secret
to be Ready before migration. Do not use `SkipDryRunOnMissingResource` to hide
missing ESO CRDs.

An ApplicationSet is useful for generating identical *workload* Applications
across many lower environments. It is intentionally not used for migration
here: the app-of-apps pattern makes the per-environment bundle ID, database
backup, manual sync, completion evidence, and promotion gate explicit. Never
generate a continuously self-healing migration Application.

## Secret ownership

Choose one owner for each target Secret:

- Hand-managed mode: a cluster operator creates and rotates the
  `jeen-insights-secrets` Secret and `jeen-insights-idp-ca` ConfigMap; keep
  `global.externalSecrets.enabled=false`. Argo only references their names.
- ESO mode: the platform team owns the ESO CRDs, controller, store, and remote
  secret values. Argo may own an `ExternalSecret` reference, but never renders
  `Secret.data` or `Secret.stringData`. Use `creationPolicy: Merge` only when a
  hand-managed Secret has a clearly documented split-key ownership contract;
  otherwise one controller must own the whole Secret.

After rotation, change `global.secretVersion` by reviewed PR to restart pods
without exposing secret contents. The IdP CA ConfigMap and mount must remain
operator-owned according to the selected environment profile.

## Migration Application: wave -1

[`applications/migration.yaml`](applications/migration.yaml) sources the same
canonical [`deployment/k8s_dev`](../k8s_dev/) chart as workloads. Its inline
values disable every workload and ExternalSecret, enable only migration, set an
immutable API image and DNS-safe bundle ID, and leave
`ttlSecondsAfterFinished: null`.

The migration is a regular, versioned Kubernetes Job. It is not a Helm hook or
an Argo hook. The Application has no automated sync, self-heal, force, or
replace option. Sync it manually **without prune**, wait for `Complete`, inspect
and retain logs, and stop promotion on failure.

An unchanged second sync does not rerun the Job: the stable bundle ID produces
the same Job name, no TTL controller deletes it, and
`ApplyOutOfSyncOnly=true` avoids reapplying an already-synced object. Never
delete a completed Job merely to make it run again.

Each promotion creates a new immutable image reference and a new DNS-safe
`migration.bundleId` in a reviewed pull request. That renders a new Job name.
Prune older Jobs only after the new Job succeeds and required logs, checksums,
audit evidence, and retention periods are satisfied. A Git or workload rollback
does not reverse database changes; use the forward-only recovery process in the
migration runbook.

## Workload Application: wave 0

[`applications/workloads-lower.yaml`](applications/workloads-lower.yaml) is a
lower-environment example that intentionally has no automated sync. It pins the
Git revision and all component image tags, explicitly disables workload-start
migration/bootstrap, keeps `migration.enabled=false`, and keeps chart-owned
ExternalSecret rendering disabled so the separate bootstrap phase remains the
only owner. The same workload values work with a hand-managed Secret when the
bootstrap Application is limited to namespace preparation. Manually sync
workloads only after the corresponding migration Job succeeds and its logs are
reviewed.

If a platform creates a separate lower-environment automated workload or
ApplicationSet example, do not add it to this default bootstrap
`kustomization.yaml`. Its promotion controller must externally verify Secret
readiness and the exact migration Job's successful completion, or the Argo
installation must provide and test child-Application health propagation. Never
use the existence or sync wave of the migration Application as completion
evidence. Defence, production, regulated, and air-gapped environments remain
manual.

When an HPA owns replicas, do not also make Git continuously enforce
`replicaCount`. Either omit/ignore `/spec/replicas` for HPA-managed Deployments
using a narrowly scoped Argo `ignoreDifferences`, or keep autoscaling disabled.
Review replica drift before enabling self-heal; broad ignore rules can hide
real configuration drift.

## Sync, health, and rollback

For an initial install or promotion:

1. Confirm ESO/store/Secret readiness and namespace ownership.
2. Confirm the database backup.
3. Refresh and manually sync the migration Application without prune.
4. Wait for the Job to complete and retain its logs and checksum evidence.
5. Sync the workload Application; automated sync is allowed only for approved
   lower environments.
6. Verify Argo health, Ready workloads, API baseline verification, UI/API health
   endpoints, login, and a governed query.

If workload health fails, stop automated sync if enabled and revert the workload
Git change through a reviewed PR. Argo can restore the previous workload
manifests, but it cannot roll back schema changes. Follow forward-only database
recovery and do not force-sync, replace, or delete the completed migration Job.

Before deleting an Application, decide whether resources must be orphaned or
pruned and verify no other controller will adopt them. `FailOnSharedResource`
is a guardrail, not a substitute for that ownership review.
