#!/usr/bin/env bash
# Fast chart/docs-only exercise of the air-gapped OpenShift bundle pipeline.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

"${ROOT}/deployment/openshift/build-images.sh" \
  --tag fixture \
  --out "${tmp_dir}/fixture" \
  --namespace jeen-insights \
  --no-images \
  --no-bundle \
  --security-artifacts off

"${tmp_dir}/fixture/validate-bundle.sh" "${tmp_dir}/fixture"

printf 'OpenShift air-gap fixture validation passed.\n'
