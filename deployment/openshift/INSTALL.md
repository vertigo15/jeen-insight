# Install Jeen Insights on OpenShift (air-gapped)

This is the customer-site procedure. The three application images are
already built for `linux/amd64` and handed over as gzipped tarballs. Jeen
Schema Modeler (and its metadata PostgreSQL) is already installed in the
same environment — the migration Job adds Insights' own `insights_*` tables
to that shared database before workloads start.

Nothing in this flow needs cluster-admin after the internal registry route
is exposed, and nothing needs Docker root on the bastion.

## What you receive

The extracted hand-off is self-contained. It includes all three
`jeen-insights-<component>_<tag>.tar.gz` image archives, their requirements
locks, `values.openshift.yaml`, `values.migration.example.yaml`, the packaged
`jeen-insights-<chart-version>.tgz` umbrella chart with vendored dependencies,
`manifests.yaml`, `migration-job.yaml`, environment examples, checksums, and
human/machine-readable manifests. Canonical configuration, migration, and
identity references are included under `docs/`. The source overlays are
[`../k8s_dev/values.openshift.yaml`](../k8s_dev/values.openshift.yaml) and
[`../k8s_dev/values.migration.example.yaml`](../k8s_dev/values.migration.example.yaml).

Run [`validate-bundle.sh`](validate-bundle.sh) before using the hand-off. It
does not contact a chart repository, image registry, cluster, or the internet.

## Helm umbrella chart — yes

There is a Helm umbrella chart. The bundle contains it as
`jeen-insights-<chart-version>.tgz` and vendors three subcharts:

| Subchart | What it deploys |
| --- | --- |
| `jeen-insights-api` | Deployment + Service + ConfigMap |
| `jeen-insights-ui` | Deployment + Service + ConfigMap + OpenShift Route |
| `jeen-insights-analytics` | Deployment + Service + ConfigMap + NetworkPolicy (API in, no egress) |

Use the packaged chart with the bundled `values.openshift.yaml`. Do not fetch
dependencies or run `helm dependency update` on the air-gapped side.

The OpenShift overlay:

- pulls images from the in-cluster registry
- leaves `runAsUser` / `fsGroup` unset (`restricted-v2` assigns the UID)
- creates a Route (TLS edge, 360 s timeout for streaming answers)
- does not create a Secret (you create `jeen-insights-secrets` yourself)
- sets `RUN_MIGRATIONS_ON_START=false` and
  `SCHEMA_BOOTSTRAP_ON_START=false`; the chart's separately rendered,
  non-hook Job applies the `insights_*` DDL. Some revisions add
  `auth_users` columns the UI reads at sign-in, including per-user locale and
  date format, so run all bundled migrations before the first login after
  upgrading.

## Prerequisites (customer site)

- OpenShift 4.11+, x86_64 workers, a project, a user with `edit`.
- Quota covering roughly 3 CPU / 3.5 GiB limits (API 1/1Gi, UI 1/1Gi,
  sandbox 1/1.5Gi).
- Internal registry default route exposed (one-time, cluster admin):

  ```sh
  oc patch configs.imageregistry.operator.openshift.io/cluster \
    --type merge -p '{"spec":{"defaultRoute":true}}'
  ```

- Schema Modeler already running; its metadata database reachable from the
  project. Prefer a dedicated migration role with only the DDL/DML required
  for Insights-owned objects plus read access to `metadata_*` and `admin_*`.
  Back up and verify the shared database before migration.
- Bastion tools: `oc` (logged in), either `skopeo` (preferred) or `podman`,
  Helm 3.x, Python 3, and standard `tar`/checksum tools.

## 1. Prepare registry and images

```sh
./validate-bundle.sh
NS="$(oc project -q)"
./push-images.sh --namespace "$NS"
```

If the registry route uses an internal CA the bastion does not trust, add
`--tls-verify false` to `push-images.sh`.

Pods pull from
`image-registry.openshift-image-registry.svc:5000/<project>/jeen-insights-<name>:<tag>`
with no pull secret (same namespace).

## 2. Configure values and secrets

Copy `secrets.env.example` to `secrets.env` on the bastion (never commit it).
Fill in the Schema Modeler database and generate the four keys:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # three times
# → FLASK_SECRET_KEY, INTERNAL_API_SECRET, INTERNAL_ANALYTICS_SECRET
python3 -c "import base64,os; print(base64.b64encode(os.urandom(32)).decode())"
# → APP_ENCRYPTION_KEY   (back this up; losing it loses stored connector secrets)
```

`INTERNAL_ANALYTICS_SECRET` must differ from `INTERNAL_API_SECRET`.
`SETUP_BOOTSTRAP_TOKEN` is any long random string — you type it once on
`/setup`. Leave `AZURE_OPENAI_*` unset.

```sh
oc -n "$NS" create secret generic jeen-insights-secrets \
    --from-env-file=secrets.env --dry-run=client -o yaml | oc apply -f -
shred -u secrets.env
```

`--from-env-file` rules: `KEY=VALUE`, no quotes, no spaces around `=`, no
inline comments.

## 3. Preflight

Validate the transferred bundle before touching the cluster:

```sh
./validate-bundle.sh
CHART="$(awk -F '\t' '$1 == "chart" { print $2; exit }' bundle-manifest.tsv)"
helm lint --with-subcharts "$CHART" --values values.openshift.yaml
helm template jeen-insights "$CHART" \
  --namespace "$NS" --values values.openshift.yaml \
  > /tmp/jeen-insights-openshift.yaml
oc -n "$NS" apply --dry-run=server -f /tmp/jeen-insights-openshift.yaml
oc -n "$NS" get secret jeen-insights-secrets
```

Confirm image stream tags, database reachability, quota, Route host/TLS policy
and that `PUBLIC_APP_URL` exactly matches the Route origin.

## 4. Run migration

The supplied manifests are ready to apply using the namespace and registry
recorded at build time. To customize them, edit `values.openshift.yaml` so the
Route host and browser URL match (same hostname):

- `jeen-insights-ui.env.PUBLIC_APP_URL`
- `jeen-insights-ui.route.host`

Then:

```sh
CHART="$(awk -F '\t' '$1 == "chart" { print $2; exit }' bundle-manifest.tsv)"
TAG="$(awk -F= '$1 == "TAG" { print $2; exit }' images.env)"
REG_INTERNAL=image-registry.openshift-image-registry.svc:5000
BUNDLE_ID=replace-with-dns-safe-immutable-release-id
helm template jeen-insights "$CHART" \
  --namespace "$NS" \
  --values values.openshift.yaml \
  --values values.migration.example.yaml \
  --set-string migration.bundleId="${BUNDLE_ID}" \
  --set-string migration.image.repository="${REG_INTERNAL}/${NS}/jeen-insights-api" \
  --set-string migration.image.tag="${TAG}" \
  --set-string migration.existingSecret.name=jeen-insights-secrets \
  --show-only templates/migration-job.yaml \
  > migration-job.yaml
oc -n "$NS" apply -f migration-job.yaml
oc -n "$NS" wait --for=condition=complete \
  "job/jeen-insights-migrate-${BUNDLE_ID}" --timeout=15m
oc -n "$NS" logs "job/jeen-insights-migrate-${BUNDLE_ID}"

# Continue only after the migration log is reviewed.
```

See the canonical [migration runbook](../migrations.md) for backup, checksum,
shared-database and recovery policy.

## 5. Install or upgrade

```sh
helm upgrade --install jeen-insights "$CHART" \
  --namespace "$NS" \
  --values values.openshift.yaml \
  --set-string jeen-insights-api.image.repository="${REG_INTERNAL}/${NS}/jeen-insights-api" \
  --set-string jeen-insights-ui.image.repository="${REG_INTERNAL}/${NS}/jeen-insights-ui" \
  --set-string jeen-insights-analytics.image.repository="${REG_INTERNAL}/${NS}/jeen-insights-analytics" \
  --set-string jeen-insights-api.image.tag="${TAG}" \
  --set-string jeen-insights-ui.image.tag="${TAG}" \
  --set-string jeen-insights-analytics.image.tag="${TAG}" \
  --wait --timeout 10m
```

`REG_INTERNAL` is always the in-cluster address:

```
image-registry.openshift-image-registry.svc:5000
```

(`$REG` from `oc registry info --public` is only for the bastion push.)

If `helm` is not available, render on a machine that has it and copy the
YAML:

```sh
helm template jeen-insights "$CHART" \
  --namespace "$NS" \
  --values values.openshift.yaml \
  --set-string jeen-insights-api.image.repository=... \
  --set-string jeen-insights-api.image.tag="${TAG}" \
  # ... same for ui and analytics \
  > manifests.yaml

# Render/apply/wait/log migration-job.yaml first using the migration-only
# command above, then:
oc -n "$NS" apply -f manifests.yaml
```

```sh
oc -n "$NS" rollout status deployment/jeen-insights-api \
  deployment/jeen-insights-ui deployment/jeen-insights-analytics
oc -n "$NS" get route jeen-insights-ui
```

The API log should report that the baseline was verified. Only the UI is
exposed.

### First run

1. Open `https://<route host>/setup`, enter `SETUP_BOOTSTRAP_TOKEN`, create
   the first admin (local accounts only — Entra SSO is off).
2. In Schema Modeler, register the on-prem LLM (`vllm` or `remote`, with
   `baseURL`). In Insights: Settings → AI Models → set it active.
3. Settings → Connections: pick a source already curated in Schema Modeler.

### Upgrade and rollback

Push new tars under a new tag, preflight them, back up the database, and
apply/wait/log a migration Job with a new immutable bundle ID. Then run the
same `helm upgrade` with new image tags. `helm rollback` affects workloads
only; it never reverses schema changes. Confirm schema compatibility first.

## 6. Verify and troubleshoot

```sh
oc -n "$NS" exec deploy/jeen-insights-api -- \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').status)"
oc -n "$NS" exec deploy/jeen-insights-analytics -- \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8100/health').read())"
curl -k "https://$(oc -n "$NS" get route jeen-insights-ui -o jsonpath='{.spec.host}')/health"
```

| Symptom | Fix |
| --- | --- |
| `ImagePullBackOff` | Tag / repository mismatch vs what was pushed (`oc get istag`). |
| Pod rejected: `runAsUser` / SCC | Manifests were rendered without the bundled `values.openshift.yaml`. Re-install with that overlay. |
| CrashLoop: weak/missing key | `JEEN_DEV_MODE=false` fail-closed. Fix `secrets.env`, re-apply the Secret, restart. |
| CrashLoop: `not been initialised by Jeen Schema Modeler` | `METADATA_DB_*` is not the Schema Modeler metadata database. |
| “no LLM is configured” | Model not registered / not active (step 4.2). |
| Answers cut off ~60 s | Route must keep `haproxy.router.openshift.io/timeout: 360s`. |

## What stays off (no internet egress)

Entra SSO, connectors (Graph / Slack / Jira / Tavily / Power BI), and map
tiles. Local accounts and Schema Modeler connections only. CodeMirror syntax
highlighting currently loads optional modules from `esm.sh`; while those
modules are not vendored, the SQL editor gracefully falls back to a plain code
block offline.
