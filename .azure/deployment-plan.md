# Azure Container Apps deployment plan

> **Status:** Deployed

## 1. Project Overview
**Goal:** Roll out commit `27f9776c4aac2d3edd6fe944b1e09a260a258a6a` to the
existing Jeen Insights API and UI Container Apps.

**Path:** Update existing deployment.

## 2. Requirements
| Attribute | Value |
|---|---|
| Classification | Development |
| Scale | Existing configuration unchanged |
| Budget | Existing infrastructure; image builds, revisions, and one Key Vault secret |
| Subscription | Jeen Subscription (`c4289eb9-2fb6-48b7-9a75-1251ebba3992`) |
| Location | West Europe |

## 3. Components
| Component | Type | Image source | Container App |
|---|---|---|---|
| API | FastAPI | `Dockerfile` | `jeen-insights-api` (internal ingress, port 8000) |
| UI | Flask/Gunicorn | `Dockerfile.ui` | `jeen-insights-ui` (external ingress, port 8501) |
| MCP token key | Key Vault secret | Generated at deployment | `jeen-api-kv-dev/jeen-insights-mcp-kek` |

## 4. Deployment Recipe
**Selected:** Azure CLI + Azure Container Registry.

Build the immutable API/UI images from the approved commit, push them to
`jeendevregistry.azurecr.io`, then update the two existing apps in
`jeen-rg-dev-weu`. The API reads `APP_ENCRYPTION_KEY` from Key Vault through
a system-assigned managed identity; the UI runs with hardened settings and
does not receive the key.

## 5. Architecture
| Component | Azure Service | Existing resource |
|---|---|---|
| API | Azure Container Apps | `jeen-insights-api` |
| UI | Azure Container Apps | `jeen-insights-ui` |
| Registry | Azure Container Registry | `jeendevregistry.azurecr.io` |
| Encryption | Azure Key Vault | `jeen-api-kv-dev` |
| Observability | Log Analytics / Container Apps environment | Existing `victoriousdesert-e48a62c2` environment |

## 6. Provisioning Limit Checklist
No scale capacity or new resource containers are provisioned. This rollout
creates revisions, a Key Vault secret, and an API managed identity while
preserving existing resource allocations. Subscription quota is unchanged.

## 7. Execution Checklist
- [x] Confirm subscription, region, resource group, and app targets.
- [x] Confirm user approval to replace active revisions.
- [x] Build API and UI images from commit `27f9776`.
- [x] Push Linux/AMD64 immutable images to `jeendevregistry.azurecr.io`.
- [x] Validate images, offline quality gates, and existing app configuration.
- [x] Update status to `Ready for Validation`.
- [x] Record validation proof and update status to `Validated`.
- [x] Deploy API revision.
- [x] Run ordered Insights database migrations from the deployed API revision.
- [x] Verify API health before UI rollout.
- [x] Configure Key Vault-backed MCP token encryption and API managed identity.
- [x] Deploy hardened UI revision.
- [x] Deploy revisions and verify API/UI health.

## 8. Validation Proof
- Source commit: `27f9776c4aac2d3edd6fe944b1e09a260a258a6a`.
- API image: `jeendevregistry.azurecr.io/jeen-insights-api@sha256:1517be29c574a7e340feafc5a20a83f2c52e2ed96efdabc26fc556ee7204f94c`.
- Initial UI image:
  `jeendevregistry.azurecr.io/jeen-insights-ui@sha256:4eb7c85cecd18c501e8aac4c15405546eacdec5b73a89e2b28dd91249a9a6c1c`.
- Images were built by ACR for Linux/AMD64; local Apple Silicon images were not used.
- Offline NL2SQL gates: safety 10/10, groundedness 4/4, continuity 1/1,
  equivalence 1/1.
- Pre-rollout revisions: API `jeen-insights-api--0000045` and UI
  `jeen-insights-ui--0000043`, both `Running` and `Healthy`.
- Migrations `020` through `024` applied successfully.
- The Key Vault encryption round trip passed with `JEEN_DEV_MODE=false`.
- Final API revision: `jeen-insights-api--secure-27f9776`, `Running` and
  `Healthy`, with 100% traffic.
- Final UI revision: `jeen-insights-ui--securefix-27f9776`, `Running` and
  `Healthy`, with 100% traffic. It uses
  `jeendevregistry.azurecr.io/jeen-insights-ui@sha256:b9030601ab1742a09ae0edc97878b899121f38bea67573221596924a0250d474`;
  this image includes the UI route-startup correction.
