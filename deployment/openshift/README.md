# Jeen Insights on air-gapped OpenShift

**Customer install (images already built, Schema Modeler already there):**
see [INSTALL.md](INSTALL.md).

The customer install follows the common six-step operator sequence:
prepare registry/images, configure values/secrets, preflight, run migration,
install/upgrade, and verify. Use the canonical
[configuration](../configuration.md), [migration](../migrations.md), and
[OIDC](../oidc.md) references for policy details; this page preserves the
connected-side air-gap build and hand-off procedure.

Delivery of the three Jeen Insights images (API, UI, ML-skills sandbox) as
tarballs plus the environment they need, for an OpenShift cluster with no
internet access. Nothing in this flow requires cluster-admin, root, or a
Docker daemon: images are built rootless on a connected machine, carried over
as files, and pushed into the project's slice of the internal registry with a
normal user token.

```
connected build box                    air-gapped bastion            OpenShift project
build-images.sh ──► dist/openshift/ ──► push-images.sh ──► internal registry ──► Deployments
                    (tars, env, yaml)   secrets.env      ──► Secret / ConfigMaps
```

## Bundle contents

`build-images.sh` writes everything below into `dist/openshift/<tag>/` and
wraps that directory into one hand-off file,
`dist/openshift/jeen-insights-openshift-<tag>.tar` (+ `.sha256`). Carry the
single tar to the bastion and extract it there.

| File | Purpose |
| --- | --- |
| `jeen-insights-{api,ui,analytics}_<tag>.tar.gz` | Images (docker-archive, linux/amd64, `gzip -t` verified) |
| `jeen-insights-*_<tag>.requirements.lock` | Exact `pip freeze --all` of each image, for your security review |
| `SHA256SUMS`, `MANIFEST.txt`, `bundle-manifest.tsv`, `images.env` | Integrity plus human/machine-readable build, chart, and image metadata |
| `jeen-insights-<chart-version>.tgz` + `.sha256` | Packaged umbrella chart with all three dependencies vendored for offline Helm |
| `push-images.sh` | Loads the tars into the internal registry |
| `validate-bundle.sh` | Verifies checksums, image metadata, docs, and offline Helm lint/render |
| `secrets.env.example`, `config.env.example` | The complete environment: secret and non-secret halves |
| `docs/{configuration,migrations,oidc}.md` | Canonical deployment configuration, migration, and identity references |
| `manifests.yaml` | Rendered Deployments/Services/ConfigMaps/Route (`oc apply -f`) |
| `migration-job.yaml` | Migration-only Job rendered from the packaged chart; apply before workloads |
| `values.openshift.yaml`, `values.migration.example.yaml` | Canonical overlays used for the two renders |
| `*.sbom.spdx.json`, `*.scan.grype.json` | Optional standalone Syft/Grype outputs when those tools are available |

No secrets are ever written into the bundle; the `*.env.example` files are
templates, and the pods refuse to start (`JEEN_DEV_MODE=false`) when a
required key is blank or still a placeholder.

## What is inside the images

Built for a security review against the `restricted-v2` SCC:

- Numeric non-root `USER 1001:0`; every file owned `1001:0` with `g=u`
  permissions, so the arbitrary UID OpenShift assigns (always in GID 0) can
  read the tree. No `runAsUser`/`fsGroup` is pinned in the OpenShift manifests.
- Read-only root filesystem: the only writable path is `/tmp` (emptyDir);
  `HOME`, matplotlib and numba caches point there. Bytecode is pre-compiled.
- Multi-stage builds: compilers and headers stay in the builder; `pip`,
  `wheel` and the base image's `setuptools` are removed from the runtime
  layer (the venv keeps a current `setuptools` for libraries that import
  `pkg_resources`).
- Ports 8000 / 8501 / 8100 (>1024, no `NET_BIND_SERVICE` needed); all
  capabilities dropped, `allowPrivilegeEscalation: false`, seccomp
  `RuntimeDefault`.
- Everything is runtime-configured through env; there are no build-time
  arguments, so one image serves every environment. The API image carries
  `db/migrations`; the separately rendered Job runs them with an advisory lock
  before workload pods start.
- Only the three application images are shipped: PostgreSQL (the Schema
  Modeler metadata database) and the LLM are the customer's.

## 0. Prerequisites

Cluster / customer side:

- OpenShift 4.11+ with x86_64 worker nodes. Pods run under the default
  `restricted-v2` SCC; no custom SCC is needed (arbitrary UID, read-only root
  filesystem, all capabilities dropped).
- A project (namespace) and a user with `edit` on it. Resource quota to cover:
  requests 0.75 CPU / 1.8 GiB, limits 3 CPU / 3.5 GiB (API 1 CPU / 1 GiB,
  UI 1 CPU / 1 GiB, sandbox 1 CPU / 1.5 GiB).
- **Internal registry route exposed** (one-time, cluster admin):
  `oc patch configs.imageregistry.operator.openshift.io/cluster --type merge -p '{"spec":{"defaultRoute":true}}'`
- **The Jeen Schema Modeler metadata database.** Jeen Insights is not
  standalone: it reads the curated catalogue (`metadata_sources`,
  `metadata_tables`, `metadata_columns`, `knowledge_pairs`, ...) and the LLM
  registry (`admin_models`, `admin_providers`, `admin_models_providers`) that
  Schema Modeler provisions with its `sql/init-metadata-db.sql`. Deploy the
  Schema Modeler bundle first (or at least initialise its database) and point
  `METADATA_DB_*` at that same PostgreSQL database. The migration Job adds
  Insights' own `insights_*` / `app_settings` tables there; it needs no
  extensions or superuser of its own. It refuses to start against a database
  without those tables and says so in the log.
- An OpenAI-compatible LLM endpoint reachable from the API pod (vLLM, Ollama,
  LM Studio, an internal gateway...), registered as a model in Schema Modeler's
  admin. Until a model is registered and marked active, the app starts but
  every question answers with "no LLM is configured".
- Bastion tools: `oc` (logged in) and either `skopeo` (preferred) or `podman`.

Connected build box: `podman` (rootless is fine) or Docker Desktop, `git`, and
Helm 3. Helm is required because every hand-off includes and validates the
packaged chart and both rendered manifests.

## 1. Build the bundle (connected side)

```sh
# from the repo root, on a clean tree; tag defaults to the git short SHA
export PROJECT=replace-with-project
deployment/openshift/build-images.sh --namespace "$PROJECT" --tag 2026.09.1
```

The script always builds `linux/amd64` (Apple Silicon included) and refuses to
export an image whose `Os/Architecture` differs, uses `docker buildx build
--provenance=false --sbom=false --load` when buildx is present (attestation
manifests break older `podman load`/`skopeo`), and warns when the working tree
is dirty because those changes end up inside the images.

If `syft` and/or `grype` are installed, the script writes separate SBOM and
scan files; it never enables OCI/buildx attestations. Missing optional tools
only warn. Use `--require-security-tools` when both outputs are mandatory.

Before building, set the two site-specific values in the canonical OpenShift
overlay (`values.openshift.yaml` in the completed bundle) so `manifests.yaml` is ready to
apply: `jeen-insights-ui.env.PUBLIC_APP_URL` and `jeen-insights-ui.route.host`
(the same hostname, under the cluster's `*.apps.<domain>`). They can also be
edited in the rendered `manifests.yaml` on the bastion (ConfigMap
`jeen-insights-ui` and the `Route`).

## 2. Push the images (bastion)

```sh
export TAG=replace-with-immutable-tag
export PROJECT=replace-with-project
sha256sum -c "jeen-insights-openshift-${TAG}.tar.sha256"
tar -xf "jeen-insights-openshift-${TAG}.tar"
cd "$TAG"
./validate-bundle.sh
oc login ...                      # a normal user with edit on the project
./push-images.sh --namespace "$PROJECT"
```

The script verifies `SHA256SUMS`, logs in to the registry route with your
`oc whoami -t` token, streams each tar with `skopeo copy` (or `podman
load/tag/push`) and lists the resulting image stream tags. If the registry
route uses an internal CA that the bastion does not trust, add
`--tls-verify false`.

Pods reference the images by their in-cluster address, which needs no pull
secret inside the same namespace:
`image-registry.openshift-image-registry.svc:5000/<project>/jeen-insights-api:<tag>`.

## 3. Secrets and configuration

```sh
cp secrets.env.example secrets.env     # fill in; see the comments in the file
export PROJECT=replace-with-project
oc -n "$PROJECT" create secret generic jeen-insights-secrets \
    --from-env-file=secrets.env --dry-run=client -o yaml | oc apply -f -
shred -u secrets.env                    # or otherwise remove it from the bastion
```

`secrets.env` holds the PostgreSQL credentials, four generated keys
(`FLASK_SECRET_KEY`, `INTERNAL_API_SECRET`, `INTERNAL_ANALYTICS_SECRET`,
`APP_ENCRYPTION_KEY`) and the `SETUP_BOOTSTRAP_TOKEN` for first login. Back up
`APP_ENCRYPTION_KEY`; it cannot be regenerated without losing stored connector
secrets.

The non-secret settings are already inside `manifests.yaml` as three
ConfigMaps (`jeen-insights-api`, `jeen-insights-ui`, `jeen-insights-analytics`);
`config.env.example` documents every key. `JEEN_DEV_MODE=false` is set: the
pods refuse to start with a blank or placeholder key, which is the intended
fail-closed behaviour.

## 4. Deploy

```sh
export PROJECT=replace-with-project
BUNDLE_ID="$(awk -F '\t' '$1 == "bundle" && $2 == "migration_bundle_id" { print $3; exit }' bundle-manifest.tsv)"
[ -n "$BUNDLE_ID" ] || { echo "bundle manifest has no migration bundle ID" >&2; exit 1; }
oc -n "$PROJECT" apply -f migration-job.yaml
oc -n "$PROJECT" wait --for=condition=complete \
  "job/jeen-insights-migrate-${BUNDLE_ID}" --timeout=15m
oc -n "$PROJECT" logs "job/jeen-insights-migrate-${BUNDLE_ID}"

# Only after the migration completes and its log is reviewed:
oc -n "$PROJECT" apply -f manifests.yaml
oc -n "$PROJECT" rollout status deployment/jeen-insights-api deployment/jeen-insights-ui deployment/jeen-insights-analytics
oc -n "$PROJECT" logs deployment/jeen-insights-api | grep -i baseline
oc -n "$PROJECT" get route jeen-insights-ui
```

`build-images.sh` renders `migration-job.yaml` from the packaged chart's
`templates/migration-job.yaml` with the OpenShift overlay and
`values.migration.example.yaml`; there is no duplicate raw Job template.
Both migration/bootstrap-on-start settings are false, so workload pods only
verify the schema. For a dedicated migration DB role, render with
`migration.existingSecret.name` pointing to its Secret.

Only the UI is exposed (Route, TLS edge, HTTP redirected to HTTPS, 360 s
timeout for streaming answers). The API and the sandbox are ClusterIP
services; the sandbox additionally has a NetworkPolicy that admits traffic
from the API pods only and allows no egress.

## 5. First run

1. Open `https://<route host>/setup`, enter `SETUP_BOOTSTRAP_TOKEN`, create
   the first admin.
2. Make sure the on-prem LLM is registered in Schema Modeler's model admin:
   a provider of type `vllm` (or `remote`) whose config carries the endpoint's
   `baseURL` (for example `http://vllm.ml.svc:8000/v1`), the model identifier
   the server expects, and an API key if it requires one. Then, in Insights,
   Settings -> AI Models: pick it as the active model. Credentials live in the
   metadata DB (`admin_models_providers`), never in env.
3. Settings -> Connections: pick the data sources (curated in Schema Modeler)
   users will query.
4. Ask a question that needs a forecast or anomaly detection to confirm the
   sandbox path (`ML_SKILLS_ENABLED=true` is the default in this overlay).

## 6. Verification and troubleshooting

Health endpoints (all return 200 when healthy; the UI's also checks the API):

```sh
export PROJECT=replace-with-project
export ROUTE_HOST="$(oc -n "$PROJECT" get route jeen-insights-ui -o jsonpath='{.spec.host}')"
oc -n "$PROJECT" exec deploy/jeen-insights-api -- python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health').status)"
oc -n "$PROJECT" exec deploy/jeen-insights-analytics -- python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8100/health').read().decode())"
curl --fail --show-error --silent "https://${ROUTE_HOST}/health"
```

| Symptom | Cause / fix |
| --- | --- |
| `ImagePullBackOff` | Tag mismatch between `manifests.yaml` and what `push-images.sh` pushed (`oc get istag`). Both come from `images.env`; re-render or `oc set image`. |
| Pod rejected: `unable to validate against any security context constraint ... runAsUser` | A UID is pinned. The bundled `values.openshift.yaml` sets `securityContext.*: null`; make sure `manifests.yaml` was rendered with it. |
| API/UI `CrashLoopBackOff`, log says a key is missing or weak | `JEEN_DEV_MODE=false` fail-closed check. Fix the value in `secrets.env`, re-create the Secret, restart the Deployment. |
| Migration Job fails with a SQL error | Wrong DB, role lacks DDL/DML, lock timeout, or connectivity failure. Inspect `oc logs job/<name>`, fix the cause, and render a new immutable bundle ID before retrying. |
| API `CrashLoopBackOff`, log reports a missing baseline | The migration Job was not completed before workloads. Run and verify it, then restart the API. |
| API log: `The metadata database has not been initialised by Jeen Schema Modeler (missing table(s): admin_models ...)` | `METADATA_DB_*` points at a database Schema Modeler never initialised. Use the Schema Modeler metadata DB, or run its `sql/init-metadata-db.sql` there first. |
| Questions answer "no LLM is configured" | Step 5.2 not done: no enabled model/credential row in `admin_models_providers`, or none is marked active. |
| Answers cut off after ~60 s | Router timeout. The Route carries `haproxy.router.openshift.io/timeout: 360s`; check it survived edits. |
| Sandbox unreachable (`ML skills unavailable`) | `oc get networkpolicy jeen-insights-analytics`; the API pods must carry labels `app.kubernetes.io/name=jeen-insights-api`, `app.kubernetes.io/instance=jeen-insights`. |
| Registry push: `no public route` | Prerequisite in section 0; or pass `--registry replace-with-host` if the route has a custom name. |
| Registry push: x509 error | Bastion does not trust the ingress CA: `--tls-verify false`, or add the CA to the bastion's trust store. |

## 7. Upgrades

Build a new bundle with a new tag, push it, apply/wait/log its new immutable
`migration-job.yaml`, then either re-apply the new `manifests.yaml` (recommended)
or, for image-only changes,
`oc set image deployment/jeen-insights-api api=replace-with-new-image-ref` and the same for
`ui`/`analytics`. Workload pods never apply schema revisions.

## 8. Not available without internet egress

- Microsoft Entra ID SSO (`login.microsoftonline.com`): use local accounts.
- Connectors (Microsoft Graph mail, Slack, Jira, Tavily, Power BI): the
  feature ships dark and should stay off.
- Map point charts (OSM/MapTiler tiles, geocoding): `OSM_*_ENABLED=false`.
- The SQL editor's syntax highlighting (CodeMirror is loaded from `esm.sh`);
  the UI falls back to a plain code block. Everything else in the UI is
  served from the image, including the fonts.
