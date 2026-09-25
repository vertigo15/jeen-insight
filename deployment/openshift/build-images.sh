#!/usr/bin/env bash
# Build a self-contained Jeen Insights bundle for an air-gapped OpenShift site.
#
# Production bundles contain linux/amd64 docker-archives for all three images,
# exact Python requirement locks, a packaged Helm umbrella chart with vendored
# dependencies, pre-rendered workload and migration manifests, install docs,
# integrity metadata, and optional standalone SBOM/scan reports.
#
# Usage:
#   deployment/openshift/build-images.sh [--tag TAG] [--out DIR]
#       [--engine podman|docker] [--platform linux/amd64]
#       [--namespace NS] [--registry HOST[:PORT]]
#       [--no-compress] [--no-bundle]
#       [--security-artifacts auto|off] [--require-security-tools]
#       [--no-images]
#
# --no-images creates a chart/docs-only validation fixture. It is intentionally
# not a deployable hand-off and exists so CI can exercise packaging offline
# without building multi-GB images.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="${REPO_ROOT}/deployment/openshift"
CHART_SOURCE="${REPO_ROOT}/deployment/k8s_dev"

TAG=""
OUT_BASE="${REPO_ROOT}/dist/openshift"
OUT_DIR=""
ENGINE=""
PLATFORM="linux/amd64"
NAMESPACE="jeen-insights"
REGISTRY="image-registry.openshift-image-registry.svc:5000"
COMPRESS=1
BUNDLE=1
BUILD_IMAGES=1
SECURITY_ARTIFACTS="auto"
REQUIRE_SECURITY_TOOLS=0
IMAGES="api ui analytics"

usage() { awk 'NR > 1 && /^set -euo/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"; }
log() { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
require_value() {
  [ -n "${2-}" ] || die "$1 requires a value"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) require_value "$1" "${2-}"; TAG="$2"; shift 2 ;;
    --out) require_value "$1" "${2-}"; OUT_DIR="$2"; shift 2 ;;
    --engine) require_value "$1" "${2-}"; ENGINE="$2"; shift 2 ;;
    --platform) require_value "$1" "${2-}"; PLATFORM="$2"; shift 2 ;;
    --namespace) require_value "$1" "${2-}"; NAMESPACE="$2"; shift 2 ;;
    --registry) require_value "$1" "${2-}"; REGISTRY="$2"; shift 2 ;;
    --security-artifacts)
      require_value "$1" "${2-}"
      SECURITY_ARTIFACTS="$2"
      shift 2
      ;;
    --require-security-tools) REQUIRE_SECURITY_TOOLS=1; SECURITY_ARTIFACTS="auto"; shift ;;
    --no-images) BUILD_IMAGES=0; shift ;;
    --no-compress) COMPRESS=0; shift ;;
    --compress) COMPRESS=1; shift ;;
    --no-bundle) BUNDLE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

case "$SECURITY_ARTIFACTS" in
  auto|off) ;;
  *) die "--security-artifacts must be 'auto' or 'off'" ;;
esac
[ "$PLATFORM" = "linux/amd64" ] \
  || die "air-gapped OpenShift bundles must target linux/amd64 (got ${PLATFORM})"
[ "$BUILD_IMAGES" = 1 ] || [ "$REQUIRE_SECURITY_TOOLS" = 0 ] \
  || die "--require-security-tools cannot be used with --no-images"

for tool in git helm tar gzip awk sed tr python3; do
  command -v "$tool" >/dev/null 2>&1 || die "${tool} not found on PATH"
done

if command -v sha256sum >/dev/null 2>&1; then
  SHA_TOOL="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
  SHA_TOOL="shasum"
else
  die "need sha256sum or shasum"
fi

checksum_file() {
  if [ "$SHA_TOOL" = "sha256sum" ]; then
    sha256sum "$1"
  else
    shasum -a 256 "$1"
  fi
}

checksum_value() {
  checksum_file "$1" | awk '{print $1}'
}

GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || printf unknown)"
GIT_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || printf unknown)"
GIT_DIRTY=0
if [ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)" ]; then
  GIT_DIRTY=1
fi

if [ -z "$TAG" ]; then
  TAG="$(git -C "$REPO_ROOT" rev-parse --short=8 HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"
  [ "$GIT_DIRTY" = 0 ] || TAG="${TAG}-dirty"
fi
case "$TAG" in
  ""|*[!A-Za-z0-9._-]*|[._-]*) die "tag must start with an alphanumeric and contain only A-Z, a-z, 0-9, '.', '_' or '-'" ;;
esac
if [ "$GIT_DIRTY" = 1 ]; then
  if [ "$BUILD_IMAGES" = 1 ]; then
    warn "working tree has uncommitted changes; production images will include them (tag ${TAG})"
  else
    warn "working tree has uncommitted changes; fixture metadata will record git_dirty=1"
  fi
fi

[ -n "$OUT_DIR" ] || OUT_DIR="${OUT_BASE}/${TAG}"
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
if [ -n "$(ls -A "$OUT_DIR" 2>/dev/null)" ]; then
  die "output directory is not empty: ${OUT_DIR}; choose a new --out directory"
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
IMAGE_RECORDS="${TMP_DIR}/images.tsv"
: > "$IMAGE_RECORDS"

file_size() {
  if stat -f%z "$1" >/dev/null 2>&1; then
    stat -f%z "$1"
  else
    stat -c%s "$1"
  fi
}

dockerfile_for() {
  case "$1" in
    api) printf '%s\n' Dockerfile ;;
    ui) printf '%s\n' Dockerfile.ui ;;
    analytics) printf '%s\n' Dockerfile.analytics ;;
    *) die "unknown image component: $1" ;;
  esac
}

BUILDX=0
if [ "$BUILD_IMAGES" = 1 ]; then
  if [ -z "$ENGINE" ]; then
    if command -v podman >/dev/null 2>&1; then
      ENGINE="podman"
    elif command -v docker >/dev/null 2>&1; then
      ENGINE="docker"
    else
      die "neither podman nor docker found; use --no-images only for validation fixtures"
    fi
  fi
  case "$ENGINE" in
    podman|docker) ;;
    *) die "--engine must be podman or docker" ;;
  esac
  command -v "$ENGINE" >/dev/null 2>&1 || die "${ENGINE} not found on PATH"
  if [ "$ENGINE" = "podman" ]; then
    if [ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null || true)" = "true" ]; then
      printf 'podman: rootless mode\n'
    fi
  elif docker buildx version >/dev/null 2>&1; then
    BUILDX=1
  fi
else
  ENGINE="none"
fi

if [ "$BUILD_IMAGES" = 1 ] && [ "$SECURITY_ARTIFACTS" = "auto" ]; then
  command -v syft >/dev/null 2>&1 || warn "syft not found; SBOM files will not be produced"
  command -v grype >/dev/null 2>&1 || warn "grype not found; vulnerability scan files will not be produced"
  if [ "$REQUIRE_SECURITY_TOOLS" = 1 ]; then
    command -v syft >/dev/null 2>&1 || die "syft is required by --require-security-tools"
    command -v grype >/dev/null 2>&1 || die "grype is required by --require-security-tools"
  fi
fi

build_image() {
  local dockerfile="$1"
  local image="$2"
  if [ "$ENGINE" = "podman" ]; then
    podman build --platform "$PLATFORM" -f "${REPO_ROOT}/${dockerfile}" -t "$image" "$REPO_ROOT"
  elif [ "$BUILDX" = 1 ]; then
    # Standalone files are used for SBOMs/scans. OCI/buildx attestations remain
    # disabled because they add unknown/unknown manifests that older
    # Podman/Skopeo versions cannot load.
    docker buildx build --platform "$PLATFORM" --provenance=false --sbom=false --load \
      -f "${REPO_ROOT}/${dockerfile}" -t "$image" "$REPO_ROOT"
  else
    docker build --platform "$PLATFORM" -f "${REPO_ROOT}/${dockerfile}" -t "$image" "$REPO_ROOT"
  fi
}

generate_security_artifacts() {
  local name="$1"
  local image="$2"
  local source sbom scan
  GENERATED_SBOM="-"
  GENERATED_SCAN="-"
  [ "$SECURITY_ARTIFACTS" = "auto" ] || return 0

  source="${ENGINE}:${image}"
  sbom="${OUT_DIR}/jeen-insights-${name}_${TAG}.sbom.spdx.json"
  scan="${OUT_DIR}/jeen-insights-${name}_${TAG}.scan.grype.json"

  if command -v syft >/dev/null 2>&1; then
    log "SBOM ${name} -> $(basename "$sbom")"
    if syft "$source" -o spdx-json > "$sbom"; then
      GENERATED_SBOM="$(basename "$sbom")"
    elif [ "$REQUIRE_SECURITY_TOOLS" = 1 ]; then
      die "syft failed for ${image}"
    else
      rm -f "$sbom"
      warn "syft failed for ${image}; continuing without its SBOM"
    fi
  fi

  if command -v grype >/dev/null 2>&1; then
    log "scan ${name} -> $(basename "$scan")"
    if [ "$GENERATED_SBOM" != "-" ]; then
      source="sbom:${sbom}"
    fi
    if grype "$source" -o json > "$scan"; then
      GENERATED_SCAN="$(basename "$scan")"
    elif [ "$REQUIRE_SECURITY_TOOLS" = 1 ]; then
      die "grype failed for ${image}"
    else
      rm -f "$scan"
      warn "grype failed for ${image}; continuing without its scan"
    fi
  fi
}

BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
{
  printf '# generated by build-images.sh on %s\n' "$BUILD_TIME"
  printf 'TAG=%s\n' "$TAG"
  printf 'IMAGES="%s"\n' "$IMAGES"
  printf 'IMAGES_BUILT=%s\n' "$BUILD_IMAGES"
  printf 'PLATFORM=%s\n' "$PLATFORM"
  printf 'NAMESPACE=%s\n' "$NAMESPACE"
  printf 'REGISTRY=%s\n' "$REGISTRY"
} > "${OUT_DIR}/images.env"

if [ "$BUILD_IMAGES" = 1 ]; then
  for name in $IMAGES; do
    dockerfile="$(dockerfile_for "$name")"
    image="jeen-insights-${name}:${TAG}"
    tar_path="${OUT_DIR}/jeen-insights-${name}_${TAG}.tar"
    requirements="${OUT_DIR}/jeen-insights-${name}_${TAG}.requirements.lock"

    log "build ${image} (${dockerfile}, ${PLATFORM})"
    build_image "$dockerfile" "$image"

    architecture="$("$ENGINE" image inspect --format '{{.Os}}/{{.Architecture}}' "$image")"
    [ "$architecture" = "$PLATFORM" ] \
      || die "${image} is ${architecture}, expected ${PLATFORM}"
    image_id="$("$ENGINE" image inspect --format '{{.Id}}' "$image")"
    case "$image_id" in
      sha256:*) ;;
      *) die "${ENGINE} returned an unexpected image ID for ${image}: ${image_id}" ;;
    esac
    image_digest="$image_id"

    log "requirements lock -> $(basename "$requirements")"
    "$ENGINE" run --rm --platform "$PLATFORM" --entrypoint cat "$image" /opt/venv/requirements.lock \
      > "$requirements"
    [ -s "$requirements" ] || die "requirements lock is empty for ${image}"

    generate_security_artifacts "$name" "$image"

    log "save -> $(basename "$tar_path")"
    if [ "$ENGINE" = "podman" ]; then
      podman save --format docker-archive -o "$tar_path" "$image"
    else
      docker save -o "$tar_path" "$image"
    fi

    shipped="$tar_path"
    if [ "$COMPRESS" = 1 ]; then
      gzip -9 -f "$tar_path"
      shipped="${tar_path}.gz"
      gzip -t "$shipped"
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$name" "$architecture" "$image_id" "$image_digest" \
      "$(file_size "$shipped")" "$(basename "$shipped")" \
      "$(basename "$requirements")" "${GENERATED_SBOM}:${GENERATED_SCAN}" \
      >> "$IMAGE_RECORDS"
  done
fi

log "copy install files"
for file in INSTALL.md README.md push-images.sh validate-bundle.sh secrets.env.example config.env.example; do
  [ -f "${HERE}/${file}" ] || die "required bundle source is missing: deployment/openshift/${file}"
  cp "${HERE}/${file}" "${OUT_DIR}/${file}"
done
mkdir -p "${OUT_DIR}/docs"
for file in configuration.md migrations.md oidc.md; do
  source_doc="${REPO_ROOT}/deployment/${file}"
  [ -f "$source_doc" ] || die "required deployment reference is missing: deployment/${file}"
  cp "$source_doc" "${OUT_DIR}/docs/${file}"
done
cp "${CHART_SOURCE}/values.openshift.yaml" "${OUT_DIR}/values.openshift.yaml"
cp "${CHART_SOURCE}/values.migration.example.yaml" "${OUT_DIR}/values.migration.example.yaml"
chmod +x "${OUT_DIR}/push-images.sh" "${OUT_DIR}/validate-bundle.sh"

# README.md and INSTALL.md live one level deeper in the repository than they
# do in the flattened hand-off. Rewrite only the copied documents so source
# links remain correct while every bundle link stays inside the extraction.
python3 - "$OUT_DIR" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
replacements = {
    root / "README.md": {
        "../configuration.md": "docs/configuration.md",
        "../migrations.md": "docs/migrations.md",
        "../oidc.md": "docs/oidc.md",
    },
    root / "INSTALL.md": {
        "../k8s_dev/values.openshift.yaml": "values.openshift.yaml",
        "../k8s_dev/values.migration.example.yaml": "values.migration.example.yaml",
        "../configuration.md": "docs/configuration.md",
        "../migrations.md": "docs/migrations.md",
        "../oidc.md": "docs/oidc.md",
    },
}
for path, mapping in replacements.items():
    text = path.read_text(encoding="utf-8")
    for source, bundled in mapping.items():
        text = text.replace(source, bundled)
    path.write_text(text, encoding="utf-8")
PY

log "package Helm chart with vendored dependencies"
CHART_STAGE="${TMP_DIR}/chart"
mkdir -p "$CHART_STAGE"
cp -R "${CHART_SOURCE}/." "$CHART_STAGE/"
HELM_REPOSITORY_CONFIG="${TMP_DIR}/repositories.yaml" \
HELM_REPOSITORY_CACHE="${TMP_DIR}/repository-cache" \
  helm dependency build --skip-refresh "$CHART_STAGE"
# The repository keeps file:// dependencies in source form. After Helm creates
# their archives, remove only the temporary source copies so `helm package`
# does not serialize each dependency twice.
rm -rf \
  "${CHART_STAGE}/charts/jeen-insights-api" \
  "${CHART_STAGE}/charts/jeen-insights-ui" \
  "${CHART_STAGE}/charts/jeen-insights-analytics"
CHART_VERSION="$(helm show chart "$CHART_STAGE" | awk '$1 == "version:" {print $2; exit}')"
[ -n "$CHART_VERSION" ] || die "could not read chart version"
HELM_REPOSITORY_CONFIG="${TMP_DIR}/repositories.yaml" \
HELM_REPOSITORY_CACHE="${TMP_DIR}/repository-cache" \
  helm package "$CHART_STAGE" --destination "$OUT_DIR" >/dev/null
CHART_ARCHIVE="jeen-insights-${CHART_VERSION}.tgz"
[ -s "${OUT_DIR}/${CHART_ARCHIVE}" ] || die "Helm package was not created: ${CHART_ARCHIVE}"
CHART_SHA256="$(checksum_value "${OUT_DIR}/${CHART_ARCHIVE}")"
printf '%s  %s\n' "$CHART_SHA256" "$CHART_ARCHIVE" > "${OUT_DIR}/${CHART_ARCHIVE}.sha256"

JOB_TAG="$(printf '%s' "$TAG" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed -E 's/^-+//; s/-+$//')"
[ -n "$JOB_TAG" ] || die "tag '${TAG}' cannot produce a DNS-safe migration bundle ID"
[ "${#JOB_TAG}" -le 41 ] \
  || die "migration bundle ID '${JOB_TAG}' is too long; use a tag no longer than 41 DNS-safe characters"

log "render workload manifests from packaged chart"
helm lint "${OUT_DIR}/${CHART_ARCHIVE}" \
  --values "${OUT_DIR}/values.openshift.yaml" \
  --set-string "jeen-insights-api.image.tag=${TAG}" \
  --set-string "jeen-insights-ui.image.tag=${TAG}" \
  --set-string "jeen-insights-analytics.image.tag=${TAG}"
helm template jeen-insights "${OUT_DIR}/${CHART_ARCHIVE}" \
  --namespace "$NAMESPACE" \
  --values "${OUT_DIR}/values.openshift.yaml" \
  --set-string "jeen-insights-api.image.repository=${REGISTRY}/${NAMESPACE}/jeen-insights-api" \
  --set-string "jeen-insights-ui.image.repository=${REGISTRY}/${NAMESPACE}/jeen-insights-ui" \
  --set-string "jeen-insights-analytics.image.repository=${REGISTRY}/${NAMESPACE}/jeen-insights-analytics" \
  --set-string "jeen-insights-api.image.tag=${TAG}" \
  --set-string "jeen-insights-ui.image.tag=${TAG}" \
  --set-string "jeen-insights-analytics.image.tag=${TAG}" \
  > "${OUT_DIR}/manifests.yaml"

log "render migration Job from packaged chart"
helm template jeen-insights "${OUT_DIR}/${CHART_ARCHIVE}" \
  --namespace "$NAMESPACE" \
  --values "${OUT_DIR}/values.openshift.yaml" \
  --values "${OUT_DIR}/values.migration.example.yaml" \
  --set-string "migration.bundleId=${JOB_TAG}" \
  --set-string "migration.image.repository=${REGISTRY}/${NAMESPACE}/jeen-insights-api" \
  --set-string "migration.image.tag=${TAG}" \
  --set-string "migration.existingSecret.name=jeen-insights-secrets" \
  --show-only templates/migration-job.yaml \
  > "${OUT_DIR}/migration-job.yaml"

[ -s "${OUT_DIR}/manifests.yaml" ] || die "workload manifest render is empty"
[ -s "${OUT_DIR}/migration-job.yaml" ] || die "migration Job render is empty"

MANIFEST_TSV="${OUT_DIR}/bundle-manifest.tsv"
{
  printf 'record\tkey\tvalue\tvalue2\tvalue3\tvalue4\tvalue5\tvalue6\tvalue7\n'
  printf 'bundle\tformat_version\t1\n'
  printf 'bundle\tmode\t%s\n' "$([ "$BUILD_IMAGES" = 1 ] && printf production || printf fixture-no-images)"
  printf 'bundle\tbuild_time\t%s\n' "$BUILD_TIME"
  printf 'bundle\tgit_commit\t%s\n' "$GIT_COMMIT"
  printf 'bundle\tgit_branch\t%s\n' "$GIT_BRANCH"
  printf 'bundle\tgit_dirty\t%s\n' "$GIT_DIRTY"
  printf 'bundle\ttag\t%s\n' "$TAG"
  printf 'bundle\tplatform\t%s\n' "$PLATFORM"
  printf 'bundle\tnamespace\t%s\n' "$NAMESPACE"
  printf 'bundle\tregistry\t%s\n' "$REGISTRY"
  printf 'bundle\tmigration_bundle_id\t%s\n' "$JOB_TAG"
  printf 'chart\t%s\t%s\t%s\n' "$CHART_ARCHIVE" "$CHART_VERSION" "$CHART_SHA256"
  while IFS= read -r image_record; do
    [ -n "$image_record" ] && printf 'image\t%s\n' "$image_record"
  done < "$IMAGE_RECORDS"
} > "$MANIFEST_TSV"

MANIFEST_TXT="${OUT_DIR}/MANIFEST.txt"
{
  printf 'Jeen Insights - OpenShift air-gap bundle\n'
  printf 'mode:                %s\n' "$([ "$BUILD_IMAGES" = 1 ] && printf production || printf fixture-no-images)"
  printf 'build time:          %s\n' "$BUILD_TIME"
  printf 'git commit:          %s\n' "$GIT_COMMIT"
  printf 'git branch:          %s\n' "$GIT_BRANCH"
  printf 'git dirty:           %s\n' "$GIT_DIRTY"
  printf 'tag:                 %s\n' "$TAG"
  printf 'platform:            %s\n' "$PLATFORM"
  printf 'target registry:     %s/%s\n' "$REGISTRY" "$NAMESPACE"
  printf 'chart:               %s\n' "$CHART_ARCHIVE"
  printf 'chart SHA-256:       %s\n' "$CHART_SHA256"
  printf 'migration bundle ID: %s\n' "$JOB_TAG"
  if [ "$BUILD_IMAGES" = 1 ]; then
    printf 'engine:              %s\n\n' "$("$ENGINE" --version 2>/dev/null || printf '%s' "$ENGINE")"
    printf '%-10s %-12s %-71s %12s  %s\n' name os/arch image_id bytes archive
    while IFS=$'\t' read -r name architecture image_id _digest bytes archive _requirements _security; do
      printf '%-10s %-12s %-71s %12s  %s\n' \
        "$name" "$architecture" "$image_id" "$bytes" "$archive"
    done < "$IMAGE_RECORDS"
  else
    printf '\nImages: not built; this is a CI/local packaging fixture, not a deployable hand-off.\n'
  fi
} > "$MANIFEST_TXT"

log "write SHA256SUMS"
(
  cd "$OUT_DIR"
  for file in * docs/*; do
    [ -f "$file" ] || continue
    [ "$file" = "SHA256SUMS" ] || checksum_file "$file"
  done
) > "${OUT_DIR}/SHA256SUMS"

log "validate completed bundle"
"${OUT_DIR}/validate-bundle.sh" "$OUT_DIR"

if [ "$BUNDLE" = 1 ]; then
  bundle="$(dirname "$OUT_DIR")/jeen-insights-openshift-${TAG}.tar"
  [ ! -e "$bundle" ] || die "bundle already exists: ${bundle}"
  log "bundle -> ${bundle}"
  tar -cf "$bundle" -C "$(dirname "$OUT_DIR")" "$(basename "$OUT_DIR")"
  (
    cd "$(dirname "$bundle")"
    checksum_file "$(basename "$bundle")"
  ) > "${bundle}.sha256"
fi

log "done"
cat "$MANIFEST_TXT"
if [ "$BUILD_IMAGES" = 1 ]; then
  printf '\nNext: copy the bundle and .sha256 to the bastion, extract it, run ./validate-bundle.sh, then follow INSTALL.md.\n'
else
  printf '\nFixture complete: chart/docs packaging validated without building images.\n'
fi
