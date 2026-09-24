#!/usr/bin/env bash
# Build the three Jeen Insights images for linux/amd64 and package them as
# tarballs for an air-gapped OpenShift cluster.
#
# Runs on the CONNECTED side (this needs PyPI + a base-image registry). The
# target never pulls anything. It never needs root:
#   * Linux without admin rights: rootless podman. `podman info` must show a
#     user namespace range (`/etc/subuid` + `/etc/subgid` entries for your user,
#     normally created by useradd; ask an admin once if they are missing).
#   * macOS (Docker Desktop or Podman Desktop): works out of the box; the
#     --platform flag makes the VM build x86_64 images for the cluster nodes,
#     and the script refuses to ship anything that is not linux/amd64.
#
# Build from a clean tree: whatever is in the working copy ends up inside the
# images, so uncommitted changes produce a bundle nobody can reproduce. The
# script warns and appends -dirty to the default tag.
#
# Output (default dist/openshift/<tag>/):
#   jeen-insights-<name>_<tag>.tar.gz            docker-archive, one per image
#   jeen-insights-<name>_<tag>.requirements.lock exact `pip freeze --all` of the image
#   SHA256SUMS                                   checksums of every shipped file
#   MANIFEST.txt                                 build time, git commit, image ids
#   images.env                                   consumed by push-images.sh
#   manifests.yaml                               rendered Helm output (needs helm)
#   migrate-job.yaml                             optional one-off migration Job
#   README.md, push-images.sh, *.env.example, values.openshift.yaml
# and, one level up, jeen-insights-openshift-<tag>.tar: the whole directory as
# a single file to hand over.
#
# Usage:
#   deployment/openshift/build-images.sh [--tag TAG] [--out DIR]
#       [--engine podman|docker] [--platform linux/amd64]
#       [--namespace NS] [--registry HOST[:PORT]] [--only api,ui,analytics]
#       [--no-compress] [--no-bundle] [--skip-manifests]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="${REPO_ROOT}/deployment/openshift"

TAG=""
OUT_BASE="${REPO_ROOT}/dist/openshift"
OUT_DIR=""
ENGINE=""
PLATFORM="linux/amd64"
NAMESPACE="jeen-insights"
# In-cluster address of the OpenShift internal registry; pods pull from it
# without any pull secret when the image lives in their own namespace.
REGISTRY="image-registry.openshift-image-registry.svc:5000"
ONLY="api,ui,analytics"
COMPRESS=1
BUNDLE=1
SKIP_MANIFESTS=0

usage() { awk 'NR > 1 && /^set -euo/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --engine) ENGINE="$2"; shift 2 ;;
    --platform) PLATFORM="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --registry) REGISTRY="$2"; shift 2 ;;
    --only) ONLY="$2"; shift 2 ;;
    --no-compress) COMPRESS=0; shift ;;
    --compress) COMPRESS=1; shift ;;
    --no-bundle) BUNDLE=0; shift ;;
    --skip-manifests) SKIP_MANIFESTS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

log() { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { echo "error: $*" >&2; exit 1; }

# ── Tooling ──────────────────────────────────────────────────────────────────
if [ -z "$ENGINE" ]; then
  if command -v podman >/dev/null 2>&1; then ENGINE=podman
  elif command -v docker >/dev/null 2>&1; then ENGINE=docker
  else die "neither podman nor docker found on PATH"; fi
fi
command -v "$ENGINE" >/dev/null 2>&1 || die "$ENGINE not found on PATH"

BUILDX=0
if [ "$ENGINE" = podman ]; then
  if [ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = "true" ]; then
    echo "podman: rootless mode (no admin rights needed)"
  fi
elif docker buildx version >/dev/null 2>&1; then
  BUILDX=1
fi

if command -v sha256sum >/dev/null 2>&1; then SHA256="sha256sum"
elif command -v shasum >/dev/null 2>&1; then SHA256="shasum -a 256"
else die "need sha256sum or shasum"; fi

GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
GIT_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
GIT_DIRTY=0
if [ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)" ]; then GIT_DIRTY=1; fi

if [ -z "$TAG" ]; then
  TAG="$(git -C "$REPO_ROOT" rev-parse --short=8 HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"
  [ "$GIT_DIRTY" = 1 ] && TAG="${TAG}-dirty"
fi
if [ "$GIT_DIRTY" = 1 ]; then
  warn "working tree has uncommitted changes; they will be baked into the images (tag ${TAG})"
fi

[ -n "$OUT_DIR" ] || OUT_DIR="${OUT_BASE}/${TAG}"
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

dockerfile_for() {
  case "$1" in
    api) echo "Dockerfile" ;;
    ui) echo "Dockerfile.ui" ;;
    analytics) echo "Dockerfile.analytics" ;;
    *) die "unknown image '$1' (expected api, ui or analytics)" ;;
  esac
}

file_size() {
  if stat -f%z "$1" >/dev/null 2>&1; then stat -f%z "$1"; else stat -c%s "$1"; fi
}

build_image() {  # build_image <dockerfile> <image>
  if [ "$ENGINE" = podman ]; then
    podman build --platform "$PLATFORM" -f "${REPO_ROOT}/$1" -t "$2" "$REPO_ROOT"
  elif [ "$BUILDX" = 1 ]; then
    # No provenance/SBOM attestations: they turn the saved archive into an OCI
    # index with an extra "unknown/unknown" manifest that older podman/skopeo
    # loaders reject. --load guarantees the image lands in the local store even
    # with a docker-container builder.
    docker buildx build --platform "$PLATFORM" --provenance=false --sbom=false --load \
      -f "${REPO_ROOT}/$1" -t "$2" "$REPO_ROOT"
  else
    docker build --platform "$PLATFORM" -f "${REPO_ROOT}/$1" -t "$2" "$REPO_ROOT"
  fi
}

IMAGES="$(echo "$ONLY" | tr ',' ' ')"
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

{
  printf '# generated by build-images.sh on %s\n' "$BUILD_TIME"
  printf 'TAG=%s\nIMAGES="%s"\nPLATFORM=%s\nNAMESPACE=%s\nREGISTRY=%s\n' \
    "$TAG" "$IMAGES" "$PLATFORM" "$NAMESPACE" "$REGISTRY"
} > "${OUT_DIR}/images.env"

MANIFEST="${OUT_DIR}/MANIFEST.txt"
{
  echo "Jeen Insights — OpenShift air-gap bundle"
  echo "build_time:  ${BUILD_TIME}"
  echo "git_commit:  ${GIT_COMMIT}"
  echo "git_branch:  ${GIT_BRANCH}"
  echo "git_dirty:   ${GIT_DIRTY}"
  echo "tag:         ${TAG}"
  echo "platform:    ${PLATFORM}"
  echo "engine:      ${ENGINE} $("$ENGINE" --version 2>/dev/null | head -n1)"
  echo "registry:    ${REGISTRY}/${NAMESPACE}"
  echo
  printf '%-10s %-24s %-71s %12s  %s\n' name os/arch image_id bytes file
} > "$MANIFEST"

# ── Build, verify, export ────────────────────────────────────────────────────
for name in $IMAGES; do
  df="$(dockerfile_for "$name")"
  image="jeen-insights-${name}:${TAG}"
  tar_path="${OUT_DIR}/jeen-insights-${name}_${TAG}.tar"

  log "build ${image} (${df}, ${PLATFORM})"
  build_image "$df" "$image"

  arch="$("$ENGINE" image inspect --format '{{.Os}}/{{.Architecture}}' "$image")"
  [ "$arch" = "$PLATFORM" ] || die "${image} is ${arch}, expected ${PLATFORM}; the cluster nodes cannot run it"
  image_id="$("$ENGINE" image inspect --format '{{.Id}}' "$image")"

  log "requirements lock -> $(basename "${tar_path%.tar}").requirements.lock"
  "$ENGINE" run --rm --platform "$PLATFORM" --entrypoint cat "$image" /opt/venv/requirements.lock \
    > "${OUT_DIR}/jeen-insights-${name}_${TAG}.requirements.lock"

  log "save -> $(basename "$tar_path")"
  rm -f "$tar_path" "${tar_path}.gz"
  if [ "$ENGINE" = podman ]; then
    podman save --format docker-archive -o "$tar_path" "$image"
  else
    docker save -o "$tar_path" "$image"
  fi

  shipped="$tar_path"
  if [ "$COMPRESS" = 1 ]; then
    log "gzip -9 + gzip -t"
    gzip -9 -f "$tar_path"
    shipped="${tar_path}.gz"
    gzip -t "$shipped"
  fi
  printf '%-10s %-24s %-71s %12s  %s\n' "$name" "$arch" "$image_id" "$(file_size "$shipped")" "$(basename "$shipped")" \
    >> "$MANIFEST"
done

# ── Cluster-side files ───────────────────────────────────────────────────────
log "copy runbook, push script, env templates and values overlay"
for f in README.md push-images.sh secrets.env.example config.env.example values.openshift.yaml; do
  [ -f "${HERE}/${f}" ] && cp "${HERE}/${f}" "${OUT_DIR}/"
done
chmod +x "${OUT_DIR}/push-images.sh" 2>/dev/null || true

# Job names must be DNS-1123 labels: lowercase alphanumerics and '-'.
JOB_TAG="$(printf '%s' "$TAG" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-\n' '-')"
sed -e "s|__IMAGE__|${REGISTRY}/${NAMESPACE}/jeen-insights-api:${TAG}|g" \
    -e "s|__TAG__|${JOB_TAG}|g" \
    "${HERE}/migrate-job.template.yaml" > "${OUT_DIR}/migrate-job.yaml"

# ── Render manifests (optional; the ops team may have no helm binary) ────────
if [ "$SKIP_MANIFESTS" = 0 ]; then
  if command -v helm >/dev/null 2>&1; then
    log "render manifests.yaml (namespace ${NAMESPACE}, registry ${REGISTRY})"
    helm dependency build "${REPO_ROOT}/deployment/k8s_dev" >/dev/null
    helm template jeen-insights "${REPO_ROOT}/deployment/k8s_dev" \
      --namespace "$NAMESPACE" \
      --values "${HERE}/values.openshift.yaml" \
      --set-string "jeen-insights-api.image.repository=${REGISTRY}/${NAMESPACE}/jeen-insights-api" \
      --set-string "jeen-insights-ui.image.repository=${REGISTRY}/${NAMESPACE}/jeen-insights-ui" \
      --set-string "jeen-insights-analytics.image.repository=${REGISTRY}/${NAMESPACE}/jeen-insights-analytics" \
      --set-string "jeen-insights-api.image.tag=${TAG}" \
      --set-string "jeen-insights-ui.image.tag=${TAG}" \
      --set-string "jeen-insights-analytics.image.tag=${TAG}" \
      > "${OUT_DIR}/manifests.yaml"
  else
    warn "helm not found; skipping manifests.yaml (render later with values.openshift.yaml)"
  fi
fi

# ── Checksums + single hand-off file ─────────────────────────────────────────
log "SHA256SUMS"
( cd "$OUT_DIR" && rm -f SHA256SUMS && $SHA256 -- * > SHA256SUMS )

if [ "$BUNDLE" = 1 ]; then
  bundle="$(dirname "$OUT_DIR")/jeen-insights-openshift-${TAG}.tar"
  log "bundle -> ${bundle}"
  tar -cf "$bundle" -C "$(dirname "$OUT_DIR")" "$(basename "$OUT_DIR")"
  ( cd "$(dirname "$bundle")" && $SHA256 -- "$(basename "$bundle")" > "$(basename "$bundle").sha256" )
fi

log "done"
cat "$MANIFEST"
printf '\nNext: copy the bundle to the air-gapped bastion, extract it and run ./push-images.sh there.\n'
