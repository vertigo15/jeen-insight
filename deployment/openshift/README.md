# Jeen Insights on air-gapped OpenShift

**Customer install (images already built, Schema Modeler already there):**
see [INSTALL.md](INSTALL.md).

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
| `SHA256SUMS`, `MANIFEST.txt`, `images.env` | Integrity, build time / git commit / image ids; `push-images.sh` reads `images.env` |
| `push-images.sh` | Loads the tars into the internal registry |
| `secrets.env.example`, `config.env.example` | The complete environment: secret and non-secret halves |
| `manifests.yaml` | Rendered Deployments/Services/ConfigMaps/Route (`oc apply -f`) |
| `migrate-job.yaml` | Optional one-off schema migration Job (the API migrates on start by default) |
| `values.openshift.yaml` | Helm overlay used to render `manifests.yaml`; re-render if you have `helm` |

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
  arguments, so one image serves every environment. The API image also
  carries `db/migrations` and applies them on start when
  `RUN_MIGRATIONS_ON_START=true` (advisory-locked, idempotent).
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
  `METADATA_DB_*` at that same PostgreSQL database. Insights adds its own
  `insights_*` / `app_settings` tables there on first start; it needs no
  extensions or superuser of its own. It refuses to start against a database
  without those tables and says so in the log.
- An OpenAI-compatible LLM endpoint reachable from the API pod (vLLM, Ollama,
  LM Studio, an internal gateway...), registered as a model in Schema Modeler's
  admin. Until a model is registered and marked active, the app starts but
  every question answers with "no LLM is configured".
- Bastion tools: `oc` (logged in) and either `skopeo` (preferred) or `podman`.

Connected build box: `podman` (rootless is fine) or Docker Desktop, `git`,
and `helm` if you want `manifests.yaml` rendered. See the header of
`build-images.sh` for the rootless-podman note.

## 1. Build the bundle (connected side)

```sh
# from the repo root, on a clean tree; tag defaults to the git short SHA
deployment/openshift/build-images.sh --namespace <project> [--tag 2026.09.1]
```

The script always builds `linux/amd64` (Apple Silicon included) and refuses to
export an image whose `Os/Architecture` differs, uses `docker buildx build
--provenance=false --sbom=false --load` when buildx is present (attestation
manifests break older `podman load`/`skopeo`), and warns when the working tree
is dirty because those changes end up inside the images.

Before building, set the two site-specific values in
`deployment/openshift/values.openshift.yaml` so `manifests.yaml` is ready to
apply: `jeen-insights-ui.env.PUBLIC_APP_URL` and `jeen-insights-ui.route.host`
(the same hostname, under the cluster's `*.apps.<domain>`). They can also be
edited in the rendered `manifests.yaml` on the bastion (ConfigMap
`jeen-insights-ui` and the `Route`).

## 2. Push the images (bastion)

```sh
sha256sum -c jeen-insights-openshift-<tag>.tar.sha256
tar -xf jeen-insights-openshift-<tag>.tar && cd <tag>
oc login ...                      # a normal user with edit on the project
./push-images.sh --namespace <project>
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
oc -n <project> create secret generic jeen-insights-secrets \
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
oc -n <project> apply -f manifests.yaml
oc -n <project> rollout status deployment/jeen-insights-api deployment/jeen-insights-ui deployment/jeen-insights-analytics
oc -n <project> logs deployment/jeen-insights-api | grep -i migration   # "migrations complete"
oc -n <project> get route jeen-insights-ui
```

The API pod applies the schema migrations before it starts serving
(`RUN_MIGRATIONS_ON_START=true` in its ConfigMap). If your DBA wants
migrations to run under a different role, set that key to `false`, give the
Job its own credentials and run it instead:

```sh
oc -n <project> apply -f migrate-job.yaml
oc -n <project> wait --for=condition=complete job/jeen-insights-migrate-<tag> --timeout=5m
```

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
oc -n <project> exec deploy/jeen-insights-api -- python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health').status)"
oc -n <project> exec deploy/jeen-insights-analytics -- python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8100/health').read().decode())"
curl -k https://<route host>/health
```

| Symptom | Cause / fix |
| --- | --- |
| `ImagePullBackOff` | Tag mismatch between `manifests.yaml` and what `push-images.sh` pushed (`oc get istag`). Both come from `images.env`; re-render or `oc set image`. |
| Pod rejected: `unable to validate against any security context constraint ... runAsUser` | A UID is pinned. `values.openshift.yaml` sets `securityContext.*: null`; make sure `manifests.yaml` was rendered with it. |
| API/UI `CrashLoopBackOff`, log says a key is missing or weak | `JEEN_DEV_MODE=false` fail-closed check. Fix the value in `secrets.env`, re-create the Secret, restart the Deployment. |
| API `CrashLoopBackOff`, log starts with `entrypoint: applying schema migrations` then a SQL error | Migration failed (wrong DB, role lacks CREATE, network policy). Fix and let the pod restart; applied revisions are not re-run. |
| API log: `The metadata database has not been initialised by Jeen Schema Modeler (missing table(s): admin_models ...)` | `METADATA_DB_*` points at a database Schema Modeler never initialised. Use the Schema Modeler metadata DB, or run its `sql/init-metadata-db.sql` there first. |
| Questions answer "no LLM is configured" | Step 5.2 not done: no enabled model/credential row in `admin_models_providers`, or none is marked active. |
| Answers cut off after ~60 s | Router timeout. The Route carries `haproxy.router.openshift.io/timeout: 360s`; check it survived edits. |
| Sandbox unreachable (`ML skills unavailable`) | `oc get networkpolicy jeen-insights-analytics`; the API pods must carry labels `app.kubernetes.io/name=jeen-insights-api`, `app.kubernetes.io/instance=jeen-insights`. |
| Registry push: `no public route` | Prerequisite in section 0; or pass `--registry <host>` if the route has a custom name. |
| Registry push: x509 error | Bastion does not trust the ingress CA: `--tls-verify false`, or add the CA to the bastion's trust store. |

## 7. Upgrades

Build a new bundle with a new tag, push it, then either re-apply the new
`manifests.yaml` (recommended) or, for image-only changes,
`oc set image deployment/jeen-insights-api api=<new ref>` and the same for
`ui`/`analytics`. New schema revisions are applied by the API pod on its first
start; already-applied ones are skipped.

## 8. Not available without internet egress

- Microsoft Entra ID SSO (`login.microsoftonline.com`): use local accounts.
- Connectors (Microsoft Graph mail, Slack, Jira, Tavily, Power BI): the
  feature ships dark and should stay off.
- Map point charts (OSM/MapTiler tiles, geocoding): `OSM_*_ENABLED=false`.
- The SQL editor's syntax highlighting (CodeMirror is loaded from `esm.sh`);
  the UI falls back to a plain code block. Everything else in the UI is
  served from the image, including the fonts.
