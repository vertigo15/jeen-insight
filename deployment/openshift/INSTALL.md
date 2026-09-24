# Install Jeen Insights on OpenShift (air-gapped)

This is the customer-site procedure. The three application images are
already built for `linux/amd64` and handed over as gzipped tarballs. Jeen
Schema Modeler (and its metadata PostgreSQL) is already installed in the
same environment — Insights only adds its own `insights_*` tables to that
database on first start.

Nothing in this flow needs cluster-admin after the internal registry route
is exposed, and nothing needs Docker root on the bastion.

## What you receive

| File | Image |
| --- | --- |
| `jeen-insights-api_linux-amd64.tar.gz` | FastAPI service (port 8000, ClusterIP) |
| `jeen-insights-ui_linux-amd64.tar.gz` | Flask UI (port 8501, the only Route) |
| `jeen-insights-analytics_linux-amd64.tar.gz` | ML-skills sandbox (port 8100, ClusterIP, no egress) |
| `SHA256SUMS` | checksums of the three tars |

Also in this repo (copy to the bastion with the tars, or clone the chart
tree offline):

- Helm umbrella: [`deployment/k8s_dev`](../k8s_dev) (`jeen-insights`)
- OpenShift overlay: [`values.openshift.yaml`](values.openshift.yaml)
- Secret template: [`secrets.env.example`](secrets.env.example)

## Helm umbrella chart — yes

There is a Helm umbrella chart. It lives at `deployment/k8s_dev` and
declares three local subcharts:

| Subchart | What it deploys |
| --- | --- |
| `jeen-insights-api` | Deployment + Service + ConfigMap |
| `jeen-insights-ui` | Deployment + Service + ConfigMap + OpenShift Route |
| `jeen-insights-analytics` | Deployment + Service + ConfigMap + NetworkPolicy (API in, no egress) |

The umbrella is `Chart.yaml` name `jeen-insights`, version `0.1.0`. Use it
with the OpenShift overlay — **not** `values.aks-dev.yaml` (that overlay
pins UIDs, nginx Ingress, and Azure External Secrets).

```
deployment/k8s_dev/          # umbrella
  Chart.yaml
  values.yaml                # environment-agnostic defaults
  charts/
    jeen-insights-api/
    jeen-insights-ui/
    jeen-insights-analytics/
deployment/openshift/
  values.openshift.yaml      # this environment
  secrets.env.example
```

The OpenShift overlay:

- pulls images from the in-cluster registry
- leaves `runAsUser` / `fsGroup` unset (`restricted-v2` assigns the UID)
- creates a Route (TLS edge, 360 s timeout for streaming answers)
- does not create a Secret (you create `jeen-insights-secrets` yourself)
- sets `RUN_MIGRATIONS_ON_START=true` so the API applies its `insights_*` DDL
  on start (currently through `032_saved_analysis_chart_state.sql`). Two of
  these add `auth_users` columns the UI reads at sign-in —
  `027_user_locale.sql` (`locale`, per-user interface language) and
  `031_user_date_format.sql` (per-user date format) — so run the migrations
  before the first login after upgrading.

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
  project. The Insights role needs `CREATE` on the schema plus read access
  to `metadata_*` and `admin_*`.
- Bastion tools: `oc` (logged in) and either `skopeo` (preferred) or
  `podman`. `helm` 3.x if you install via the chart (recommended).

## 1. Load the three images

```sh
sha256sum -c SHA256SUMS

NS=$(oc project -q)
REG=$(oc registry info --public)
oc registry login
# or: podman login -u "$(oc whoami)" -p "$(oc whoami -t)" "$REG"

TAG=linux-amd64   # or any tag you want in the ImageStream

for name in api ui analytics; do
  gunzip -c "jeen-insights-${name}_linux-amd64.tar.gz" > "/tmp/jeen-${name}.tar"
  skopeo copy --dest-creds "$(oc whoami):$(oc whoami -t)" \
    "docker-archive:/tmp/jeen-${name}.tar" \
    "docker://${REG}/${NS}/jeen-insights-${name}:${TAG}"
  rm -f "/tmp/jeen-${name}.tar"
done

oc get istag -n "$NS"
```

If the registry route uses an internal CA the bastion does not trust, add
`--dest-tls-verify=false` to `skopeo copy`.

Pods pull from
`image-registry.openshift-image-registry.svc:5000/<project>/jeen-insights-<name>:<tag>`
with no pull secret (same namespace).

## 2. Create the Secret

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

## 3. Install with Helm

Edit `deployment/openshift/values.openshift.yaml` so the Route host and the
browser URL match (same hostname):

- `jeen-insights-ui.env.PUBLIC_APP_URL`
- `jeen-insights-ui.route.host`

Then:

```sh
# from the repo root (or a copy of deployment/ that includes k8s_dev + openshift)
helm dependency build deployment/k8s_dev

helm upgrade --install jeen-insights deployment/k8s_dev \
  --namespace "$NS" \
  --values deployment/openshift/values.openshift.yaml \
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
helm template jeen-insights deployment/k8s_dev \
  --namespace "$NS" \
  --values deployment/openshift/values.openshift.yaml \
  --set-string jeen-insights-api.image.repository=... \
  --set-string jeen-insights-api.image.tag="${TAG}" \
  # ... same for ui and analytics \
  > manifests.yaml

oc -n "$NS" apply -f manifests.yaml
```

```sh
oc -n "$NS" rollout status deployment/jeen-insights-api \
  deployment/jeen-insights-ui deployment/jeen-insights-analytics
oc -n "$NS" get route jeen-insights-ui
```

The API log should contain `migrations complete`. Only the UI is exposed.

## 4. First run

1. Open `https://<route host>/setup`, enter `SETUP_BOOTSTRAP_TOKEN`, create
   the first admin (local accounts only — Entra SSO is off).
2. In Schema Modeler, register the on-prem LLM (`vllm` or `remote`, with
   `baseURL`). In Insights: Settings → AI Models → set it active.
3. Settings → Connections: pick a source already curated in Schema Modeler.

## 5. Verify

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
| Pod rejected: `runAsUser` / SCC | Manifests were rendered without `values.openshift.yaml`. Re-install with that overlay. |
| CrashLoop: weak/missing key | `JEEN_DEV_MODE=false` fail-closed. Fix `secrets.env`, re-apply the Secret, restart. |
| CrashLoop: `not been initialised by Jeen Schema Modeler` | `METADATA_DB_*` is not the Schema Modeler metadata database. |
| “no LLM is configured” | Model not registered / not active (step 4.2). |
| Answers cut off ~60 s | Route must keep `haproxy.router.openshift.io/timeout: 360s`. |

## What stays off (no internet egress)

Entra SSO, connectors (Graph / Slack / Jira / Tavily / Power BI), and map
tiles. Local accounts and Schema Modeler connections only.

## Upgrade

Push new tars under a new tag, then `helm upgrade` with the new
`--set-string …image.tag=`. Schema revisions apply on the next API start;
already-applied ones are skipped.
