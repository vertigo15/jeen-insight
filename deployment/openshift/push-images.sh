#!/usr/bin/env bash
# Load the Jeen Insights image tarballs into the OpenShift internal registry.
#
# Runs on the AIR-GAPPED side (a bastion that can reach the cluster). It needs
# only project-level rights on the target namespace (`edit`, or
# `registry-editor` + `view`), no cluster-admin, no root and no Docker daemon.
#
# One-time prerequisite for a cluster admin: expose the internal registry's
# default route, otherwise `oc registry info --public` has nothing to return:
#   oc patch configs.imageregistry.operator.openshift.io/cluster \
#     --type merge -p '{"spec":{"defaultRoute":true}}'
#
# Tools: oc (already logged in) plus ONE of
#   * skopeo  - preferred: streams tar -> registry with no local image store
#   * podman  - fallback: load, tag, push (rootless is fine)
#
# Run from the bundle directory produced by build-images.sh (it reads
# images.env and SHA256SUMS from the current directory).
#
# Usage:
#   ./push-images.sh [--namespace NS] [--tag TAG] [--only api,ui,analytics]
#                    [--registry HOST[:PORT]] [--tls-verify false] [--skip-checksums]

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

TAG=""
IMAGES=""
NAMESPACE=""
REGISTRY_PUBLIC=""
TLS_VERIFY="true"
SKIP_CHECKSUMS=0
ONLY=""
IMAGES_BUILT=1

# Defaults recorded by build-images.sh.
if [ -f images.env ]; then
  # shellcheck disable=SC1091
  . ./images.env
fi

usage() { awk 'NR > 1 && /^set -euo/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --only) ONLY="$2"; shift 2 ;;
    --registry) REGISTRY_PUBLIC="$2"; shift 2 ;;
    --tls-verify) TLS_VERIFY="$2"; shift 2 ;;
    --skip-checksums) SKIP_CHECKSUMS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

log() { printf '\n==> %s\n' "$*"; }
die() { echo "error: $*" >&2; exit 1; }

[ -n "$TAG" ] || die "--tag is required (images.env not found)"
[ "$IMAGES_BUILT" = 1 ] \
  || die "this is a chart/docs-only fixture; build a production bundle without --no-images"
[ -n "$ONLY" ] && IMAGES="$(echo "$ONLY" | tr ',' ' ')"
[ -n "$IMAGES" ] || IMAGES="api ui analytics"

command -v oc >/dev/null 2>&1 || die "oc not found on PATH"
oc whoami >/dev/null 2>&1 || die "not logged in: run 'oc login' first"
[ -n "$NAMESPACE" ] || NAMESPACE="$(oc project -q)"

if command -v skopeo >/dev/null 2>&1; then TOOL=skopeo
elif command -v podman >/dev/null 2>&1; then TOOL=podman
else die "need skopeo or podman on this host"; fi

# ── Integrity ────────────────────────────────────────────────────────────────
if [ "$SKIP_CHECKSUMS" = 0 ] && [ -f SHA256SUMS ]; then
  log "verify SHA256SUMS"
  if command -v sha256sum >/dev/null 2>&1; then
    grep -E "jeen-insights-.*\.tar(\.gz)?$" SHA256SUMS | sha256sum -c -
  else
    grep -E "jeen-insights-.*\.tar(\.gz)?$" SHA256SUMS | shasum -a 256 -c -
  fi
fi

# ── Target project + registry ────────────────────────────────────────────────
log "project ${NAMESPACE}"
if ! oc get project "$NAMESPACE" >/dev/null 2>&1; then
  oc new-project "$NAMESPACE" >/dev/null \
    || die "project ${NAMESPACE} does not exist and could not be created; ask an admin to create it and grant you 'edit'"
fi

if [ -z "$REGISTRY_PUBLIC" ]; then
  REGISTRY_PUBLIC="$(oc registry info --public 2>/dev/null || true)"
  [ -n "$REGISTRY_PUBLIC" ] || die "the internal registry has no public route; see the header of this script for the one-line admin fix, or pass --registry"
fi
echo "registry: ${REGISTRY_PUBLIC}"

USER_NAME="$(oc whoami)"
TOKEN="$(oc whoami -t)"
[ -n "$TOKEN" ] || die "oc whoami -t returned no token (log in with a token-bearing session)"

if [ "$TOOL" = podman ]; then
  log "podman login ${REGISTRY_PUBLIC}"
  podman login --tls-verify="$TLS_VERIFY" -u "$USER_NAME" -p "$TOKEN" "$REGISTRY_PUBLIC" >/dev/null
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# ── Push ─────────────────────────────────────────────────────────────────────
for name in $IMAGES; do
  file="jeen-insights-${name}_${TAG}.tar"
  if [ ! -f "$file" ] && [ -f "${file}.gz" ]; then
    log "gunzip ${file}.gz"
    gunzip -c "${file}.gz" > "${TMP_DIR}/${file}"
    file="${TMP_DIR}/${file}"
  fi
  [ -f "$file" ] || die "missing ${file}"

  dest="${REGISTRY_PUBLIC}/${NAMESPACE}/jeen-insights-${name}:${TAG}"
  log "push ${name} -> ${dest}"
  if [ "$TOOL" = skopeo ]; then
    skopeo copy --dest-tls-verify="$TLS_VERIFY" \
      --dest-creds "${USER_NAME}:${TOKEN}" \
      "docker-archive:${file}" "docker://${dest}"
  else
    loaded="$(podman load -q -i "$file" | tail -n1 | sed 's/^Loaded image(s)*: *//')"
    [ -n "$loaded" ] || die "podman load produced no image reference"
    podman tag "$loaded" "$dest"
    podman push --tls-verify="$TLS_VERIFY" "$dest"
  fi
done

# ── Verify ───────────────────────────────────────────────────────────────────
log "image streams in ${NAMESPACE}"
for name in $IMAGES; do
  oc -n "$NAMESPACE" get imagestreamtag "jeen-insights-${name}:${TAG}" \
    -o custom-columns='NAME:.metadata.name,DIGEST:.image.metadata.name' --no-headers
done

printf '\nIn-cluster references (already set in manifests.yaml / values.openshift.yaml):\n'
for name in $IMAGES; do
  printf '  image-registry.openshift-image-registry.svc:5000/%s/jeen-insights-%s:%s\n' "$NAMESPACE" "$name" "$TAG"
done
printf '\nNext: follow INSTALL.md: create the Secret, apply and verify migration-job.yaml, then apply manifests.yaml.\n'
