#!/usr/bin/env bash
set -euo pipefail

HELM_VERSION="3.18.4"
KUBECONFORM_VERSION="0.6.7"
KUBERNETES_VERSION="1.24.0"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHART="${ROOT}/deployment/k8s_dev"
EXPECTED="${ROOT}/deployment/tests/expected/defence-invariants.yaml"
VALIDATOR="${ROOT}/deployment/tests/validate_render.py"
HELM_BIN="${HELM_BIN:-helm}"
KUBECONFORM_BIN="${KUBECONFORM_BIN:-kubeconform}"

actual_helm="$("${HELM_BIN}" version --short 2>/dev/null || true)"
if [[ "${actual_helm}" != v${HELM_VERSION}* ]]; then
  echo "Helm ${HELM_VERSION} is required; ${HELM_BIN} reported '${actual_helm:-not found}'." >&2
  echo "Set HELM_BIN to a pinned Helm ${HELM_VERSION} executable." >&2
  exit 1
fi

if ! python3 -c "import yaml" >/dev/null 2>&1; then
  echo "PyYAML is required (CI pins PyYAML 6.0.2)." >&2
  exit 1
fi

have_kubeconform=false
if command -v "${KUBECONFORM_BIN}" >/dev/null 2>&1; then
  actual_kubeconform="$("${KUBECONFORM_BIN}" -v 2>&1)"
  if [[ "${actual_kubeconform}" != *"${KUBECONFORM_VERSION}"* ]]; then
    echo "kubeconform ${KUBECONFORM_VERSION} is required; found '${actual_kubeconform}'." >&2
    exit 1
  fi
  have_kubeconform=true
elif [[ "${REQUIRE_KUBECONFORM:-0}" == "1" ]]; then
  echo "kubeconform ${KUBECONFORM_VERSION} is required but was not found." >&2
  exit 1
else
  echo "kubeconform not found; skipping schema validation (set REQUIRE_KUBECONFORM=1 to require it)."
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT

image_tags=(
  --set-string jeen-insights-api.image.tag=validation
  --set-string jeen-insights-ui.image.tag=validation
  --set-string jeen-insights-analytics.image.tag=validation
)

render_profile() {
  local profile="$1"
  shift
  local manifest="${tmp_dir}/${profile}.yaml"
  local -a values_args=("$@")

  echo "Validating ${profile} profile"
  "${HELM_BIN}" lint --with-subcharts "${CHART}" \
    --kube-version "${KUBERNETES_VERSION}" \
    "${values_args[@]}" \
    "${image_tags[@]}"
  "${HELM_BIN}" template jeen-insights "${CHART}" \
    --namespace jeen-insights \
    --kube-version "${KUBERNETES_VERSION}" \
    "${values_args[@]}" \
    "${image_tags[@]}" \
    > "${manifest}"

  local -a semantic_args=(
    --profile "${profile}"
    --manifest "${manifest}"
  )
  if [[ "${profile}" == "defence" ]]; then
    semantic_args+=(--expected "${EXPECTED}")
  fi
  python3 "${VALIDATOR}" "${semantic_args[@]}"

  if [[ "${have_kubeconform}" == "true" ]]; then
    # Standard Kubernetes schemas do not include OpenShift Route or ESO
    # ExternalSecret. validate_render.py checks their API versions and critical
    # fields before kubeconform skips those custom kinds.
    "${KUBECONFORM_BIN}" \
      -strict \
      -summary \
      -ignore-missing-schemas \
      -kubernetes-version "${KUBERNETES_VERSION}" \
      "${manifest}"
  fi
}

expect_schema_failure() {
  local description="$1"
  shift
  local output
  echo "Validating schema rejection: ${description}"
  if output="$("${HELM_BIN}" lint "${CHART}" "${image_tags[@]}" "$@" 2>&1)"; then
    echo "Expected Helm schema validation to reject ${description}, but lint passed." >&2
    exit 1
  fi
  case "$output" in
    *"values don't meet the specifications of the schema"*|*"Additional property"*|*"must be one of"*) ;;
    *)
      echo "Helm failed for an unexpected reason while testing ${description}:" >&2
      echo "$output" >&2
      exit 1
      ;;
  esac
}

"${HELM_BIN}" dependency build --skip-refresh "${CHART}"

render_profile base --values "${CHART}/values.yaml"
render_profile aks-example --values "${CHART}/values.aks.example.yaml"
render_profile eks-example --values "${CHART}/values.eks.example.yaml"
render_profile aks-dev --values "${CHART}/values.aks-dev.yaml"
render_profile defence --values "${CHART}/values.defence.yaml"
render_profile openshift --values "${CHART}/values.openshift.yaml"
render_profile migration --values "${CHART}/values.migration.example.yaml"

digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
digest_manifest="${tmp_dir}/digest.yaml"
"${HELM_BIN}" template jeen-insights "${CHART}" \
  --namespace jeen-insights \
  --kube-version "${KUBERNETES_VERSION}" \
  --set jeen-insights-analytics.enabled=true \
  --set-string jeen-insights-api.image.digest="${digest}" \
  --set-string jeen-insights-ui.image.digest="${digest}" \
  --set-string jeen-insights-analytics.image.digest="${digest}" \
  > "${digest_manifest}"
python3 "${VALIDATOR}" --digest --manifest "${digest_manifest}"

expect_schema_failure "unknown component key" \
  --set-string jeen-insights-api.imgae.repository=typo.invalid
expect_schema_failure "unsupported ExternalName Service" \
  --set-string jeen-insights-api.service.type=ExternalName
expect_schema_failure "unsupported passthrough Route" \
  --set jeen-insights-ui.route.enabled=true \
  --set-string jeen-insights-ui.route.tls.termination=passthrough

echo "Helm deployment validation passed."
