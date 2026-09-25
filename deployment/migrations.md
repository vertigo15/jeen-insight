# Database migration runbook

Jeen Insights shares PostgreSQL metadata with Jeen Schema Modeler. Application
pods must not apply DDL in shared environments:

```yaml
global:
  env:
    RUN_MIGRATIONS_ON_START: "false"
    SCHEMA_BOOTSTRAP_ON_START: "false"
```

The canonical migration is the disabled-by-default, non-hook Helm template
`deployment/k8s_dev/templates/migration-job.yaml`. Render and run it before
every workload install or upgrade that may include schema changes.

## Before migration

1. Take a database-native backup or snapshot and verify that it can be read.
2. Confirm the API image and migration bundle ID are immutable and correspond
   to the workload release.
3. Prefer a dedicated migration Secret and PostgreSQL role. Grant only the
   DDL/DML needed for Insights-owned objects and read access needed from Schema
   Modeler metadata. Workload credentials should not gain DDL merely to run the
   Job.
4. Confirm no other migration Job is running and that the target database is
   the intended Schema Modeler metadata database.

## Render, apply, wait and inspect

Run from the repository root. Values shown below are examples or shell
placeholders, never credentials:

```sh
export KUBE_CONTEXT=replace-with-context
export NAMESPACE=jeen-insights
export RELEASE=jeen-insights
export OVERLAY=deployment/k8s_dev/values.aks.example.yaml
export BUNDLE_ID=release-20260925-a1b2c3d
export API_REPOSITORY=registry.example.invalid/jeen-insights-api
export API_TAG=release-20260925-a1b2c3d
export MIGRATION_SECRET=jeen-insights-migration

helm dependency build deployment/k8s_dev
helm template "$RELEASE" deployment/k8s_dev \
  --namespace "$NAMESPACE" \
  --values "$OVERLAY" \
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
  "job/${RELEASE}-migrate-${BUNDLE_ID}" --timeout=15m
kubectl --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" \
  logs "job/${RELEASE}-migrate-${BUNDLE_ID}"
```

Review the log before proceeding. It must show successful application or
checksum-verified skipping of each revision. Then install or upgrade workloads:

```sh
helm upgrade --install "$RELEASE" deployment/k8s_dev \
  --kube-context "$KUBE_CONTEXT" \
  --namespace "$NAMESPACE" \
  --create-namespace \
  --values "$OVERLAY" \
  --set-string jeen-insights-api.image.tag="$API_TAG" \
  --wait --timeout 10m
```

Set the UI and analytics image tags in the same command for a full release.

## Defence-safe example

This example contains the approved defence target identifiers but no secret
material. Supply immutable IDs/tags in the shell:

```sh
export BUNDLE_ID=replace-with-dns-safe-immutable-id
export API_TAG=replace-with-immutable-api-tag

helm dependency build deployment/k8s_dev
helm template jeen-insights deployment/k8s_dev \
  --namespace jeen-insights \
  --values deployment/k8s_dev/values.defence.yaml \
  --values deployment/k8s_dev/values.migration.example.yaml \
  --set-string migration.bundleId="$BUNDLE_ID" \
  --set-string migration.image.repository=acrjeendefensedev30fff5c1.azurecr.io/jeen-insights/jeen-insights-api \
  --set-string migration.image.tag="$API_TAG" \
  --set-string migration.existingSecret.name=jeen-insights-secrets \
  --show-only templates/migration-job.yaml \
  > /tmp/jeen-insights-migration.yaml

kubectl --context defence-aks-openmetadata-dev161 \
  --namespace jeen-insights apply -f /tmp/jeen-insights-migration.yaml
kubectl --context defence-aks-openmetadata-dev161 \
  --namespace jeen-insights wait --for=condition=complete \
  "job/jeen-insights-migrate-${BUNDLE_ID}" --timeout=15m
kubectl --context defence-aks-openmetadata-dev161 \
  --namespace jeen-insights logs \
  "job/jeen-insights-migrate-${BUNDLE_ID}"
```

Do not upgrade defence workloads until the final command has been reviewed and
the Job is complete.

## Concurrency and timeouts

The runner takes PostgreSQL advisory lock
`jeen_insights_schema_migrations`. `MIGRATION_LOCK_WAIT_SECONDS` bounds the
polling wait; `MIGRATION_LOCK_TIMEOUT` bounds table-lock waits; and
`MIGRATION_STATEMENT_TIMEOUT` bounds each statement. Values must be finite and
positive where required. The session pins `search_path=public` before any
history or baseline access. A timeout fails the Job; it does not skip a
revision.

Each SQL file runs in its own transaction and is recorded once in
`insights_schema_migrations`. This means completed earlier revisions remain
applied when a later revision fails.

## Append-only and checksum policy

Applied migration files are immutable. Add a new, ordered migration; never
renumber, edit or delete an applied file. The runner compares each applied
file's SHA-256 checksum with the stored checksum and fails closed on drift. If
history already exists, this preflight runs before baseline DDL and also rejects
any recorded SQL revision whose file is absent from the image.

`MIGRATION_ALLOW_CHECKSUM_DRIFT=true` is an emergency review override only. It
accepts the mismatch and skips the modified, already-applied file; it does not
apply the changed SQL or repair history. Record the approval and restore the
original file whenever possible. Historical rows with a null checksum are
accepted for backward compatibility.

## Shared Schema Modeler boundary

Migration SQL may create or alter only Insights-owned objects:
`insights_*`, `connector*`, `auth_users`, and `app_settings`. It must not alter
Schema Modeler-owned objects such as `metadata_*`, `settings_services`,
`admin_*`, or their equivalents. Reading curated metadata is permitted where
the application contract requires it.

Use forward-only expand/contract changes:

1. expand with additive schema compatible with old and new pods;
2. deploy code that can read both forms and writes the new form;
3. backfill with bounded, observable work; and
4. contract only in a later release after all readers have moved.

## Failure and recovery

1. Stop before workload upgrade and save Job logs/events.
2. Identify wrong target/credentials, lock contention, timeout, checksum drift,
   insufficient privilege, or SQL failure.
3. Do not edit an applied migration. Correct an unapplied file only if it has
   never run anywhere; otherwise append a forward repair.
4. Delete no migration history rows to force a rerun.
5. Render a new Job with a new immutable bundle ID and retry.

There is no automatic database rollback. `helm rollback`, Argo rollback, or an
older image does not reverse schema changes. Recover through a tested database
restore when safe, or ship an explicit forward repair compatible with the
currently running application.
