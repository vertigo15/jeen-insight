#!/usr/bin/env bash
# Validate an extracted Jeen Insights OpenShift air-gap bundle.
#
# Usage:
#   ./validate-bundle.sh [BUNDLE_DIRECTORY]
#
# Requires Bash, Helm 3, tar, gzip, awk, and Python 3. No registry, repository,
# cluster, or network connection is used.

set -euo pipefail

BUNDLE_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
BUNDLE_DIR="$(cd "$BUNDLE_DIR" && pwd)"

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

for tool in helm tar gzip awk python3 cmp; do
  command -v "$tool" >/dev/null 2>&1 || die "${tool} not found on PATH"
done

required_files=(
  INSTALL.md
  README.md
  push-images.sh
  validate-bundle.sh
  secrets.env.example
  config.env.example
  docs/configuration.md
  docs/migrations.md
  docs/oidc.md
  values.openshift.yaml
  values.migration.example.yaml
  images.env
  manifests.yaml
  migration-job.yaml
  MANIFEST.txt
  bundle-manifest.tsv
  SHA256SUMS
)

log "check required files"
for file in "${required_files[@]}"; do
  [ -s "${BUNDLE_DIR}/${file}" ] || die "required bundle file is missing or empty: ${file}"
done

log "verify SHA256SUMS"
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$BUNDLE_DIR" && sha256sum -c SHA256SUMS)
elif command -v shasum >/dev/null 2>&1; then
  (cd "$BUNDLE_DIR" && shasum -a 256 -c SHA256SUMS)
else
  die "need sha256sum or shasum"
fi

manifest_value() {
  local key="$1"
  awk -F '\t' -v key="$key" '$1 == "bundle" && $2 == key { print $3; exit }' \
    "${BUNDLE_DIR}/bundle-manifest.tsv"
}

mode="$(manifest_value mode)"
tag="$(manifest_value tag)"
platform="$(manifest_value platform)"
namespace="$(manifest_value namespace)"
registry="$(manifest_value registry)"
migration_bundle_id="$(manifest_value migration_bundle_id)"

[ -n "$mode" ] || die "bundle manifest has no mode"
[ -n "$tag" ] || die "bundle manifest has no tag"
[ "$platform" = "linux/amd64" ] || die "unsupported bundle platform: ${platform:-missing}"
[ -n "$namespace" ] || die "bundle manifest has no namespace"
[ -n "$registry" ] || die "bundle manifest has no registry"
[ -n "$migration_bundle_id" ] || die "bundle manifest has no migration bundle ID"

read -r chart_archive chart_version chart_sha256 < <(
  awk -F '\t' '$1 == "chart" { print $2, $3, $4; exit }' \
    "${BUNDLE_DIR}/bundle-manifest.tsv"
)
[ -n "${chart_archive:-}" ] || die "bundle manifest has no chart record"
[ "$chart_archive" = "jeen-insights-${chart_version}.tgz" ] \
  || die "chart archive name does not match its version: ${chart_archive}"
[ -s "${BUNDLE_DIR}/${chart_archive}" ] || die "chart archive is missing: ${chart_archive}"
[ -s "${BUNDLE_DIR}/${chart_archive}.sha256" ] \
  || die "chart checksum file is missing: ${chart_archive}.sha256"

if command -v sha256sum >/dev/null 2>&1; then
  actual_chart_sha256="$(sha256sum "${BUNDLE_DIR}/${chart_archive}" | awk '{print $1}')"
else
  actual_chart_sha256="$(shasum -a 256 "${BUNDLE_DIR}/${chart_archive}" | awk '{print $1}')"
fi
[ "$actual_chart_sha256" = "$chart_sha256" ] \
  || die "chart checksum disagrees with bundle-manifest.tsv"

log "inspect packaged chart"
chart_contents="$(tar -tzf "${BUNDLE_DIR}/${chart_archive}")"
case "$chart_contents" in
  *"/Chart.yaml"*) ;;
  *) die "packaged chart has no Chart.yaml" ;;
esac
case "$chart_contents" in
  *"/Chart.lock"*) ;;
  *) die "packaged chart has no Chart.lock" ;;
esac
for dependency in jeen-insights-api jeen-insights-ui jeen-insights-analytics; do
  case "$chart_contents" in
    *"/charts/${dependency}/Chart.yaml"*) ;;
    *) die "packaged chart is missing vendored dependency: ${dependency}" ;;
  esac
done

case "$mode" in
  production)
    log "validate image archive metadata"
    image_count="$(awk -F '\t' '$1 == "image" { count++ } END { print count + 0 }' \
      "${BUNDLE_DIR}/bundle-manifest.tsv")"
    [ "$image_count" -eq 3 ] || die "production bundle must contain three image records; found ${image_count}"

    for expected_name in api ui analytics; do
      image_record="$(
        awk -F '\t' -v name="$expected_name" '$1 == "image" && $2 == name { print; exit }' \
          "${BUNDLE_DIR}/bundle-manifest.tsv"
      )"
      [ -n "$image_record" ] || die "missing image record for ${expected_name}"
      IFS=$'\t' read -r _record name architecture image_id image_digest _bytes archive requirements security \
        <<< "$image_record"

      [ "$architecture" = "$platform" ] \
        || die "${name} architecture is ${architecture}, expected ${platform}"
      [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] \
        || die "${name} has invalid image ID: ${image_id}"
      [ "$image_digest" = "$image_id" ] \
        || die "${name} digest does not match its content-addressed image ID"
      case "$archive" in
        "jeen-insights-${name}_${tag}.tar"|"jeen-insights-${name}_${tag}.tar.gz") ;;
        *) die "${name} archive has unexpected name: ${archive}" ;;
      esac
      [ -s "${BUNDLE_DIR}/${archive}" ] || die "missing image archive: ${archive}"
      [ "$requirements" = "jeen-insights-${name}_${tag}.requirements.lock" ] \
        || die "${name} requirements lock has unexpected name: ${requirements}"
      [ -s "${BUNDLE_DIR}/${requirements}" ] || die "missing requirements lock: ${requirements}"

      case "$archive" in
        *.tar.gz) gzip -t "${BUNDLE_DIR}/${archive}" ;;
      esac
      python3 - "$BUNDLE_DIR/$archive" "$architecture" "$image_id" <<'PY'
import hashlib
import json
import sys
import tarfile

path, expected_platform, expected_id = sys.argv[1:]
try:
    with tarfile.open(path, "r:*") as archive:
        manifest_member = archive.extractfile("manifest.json")
        if manifest_member is None:
            raise ValueError("manifest.json is absent")
        manifest = json.load(manifest_member)
        if len(manifest) != 1:
            raise ValueError(f"expected one image, found {len(manifest)}")
        config_name = manifest[0]["Config"]
        config_member = archive.extractfile(config_name)
        if config_member is None:
            raise ValueError(f"{config_name} is absent")
        config_bytes = config_member.read()
        config = json.loads(config_bytes)
except (KeyError, OSError, tarfile.TarError, ValueError, json.JSONDecodeError) as exc:
    raise SystemExit(f"error: invalid docker archive {path}: {exc}")

actual_platform = f"{config.get('os')}/{config.get('architecture')}"
if actual_platform != expected_platform:
    raise SystemExit(
        f"error: {path} contains {actual_platform}, expected {expected_platform}"
    )
actual_id = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
if actual_id != expected_id:
    raise SystemExit(
        f"error: {path} config digest is {actual_id}, expected {expected_id}"
    )
PY

      sbom="${security%%:*}"
      scan="${security#*:}"
      [ "$sbom" = "-" ] || [ -s "${BUNDLE_DIR}/${sbom}" ] \
        || die "recorded SBOM is missing: ${sbom}"
      [ "$scan" = "-" ] || [ -s "${BUNDLE_DIR}/${scan}" ] \
        || die "recorded scan is missing: ${scan}"
    done
    ;;
  fixture-no-images)
    image_count="$(awk -F '\t' '$1 == "image" { count++ } END { print count + 0 }' \
      "${BUNDLE_DIR}/bundle-manifest.tsv")"
    [ "$image_count" -eq 0 ] || die "no-image fixture unexpectedly contains image records"
    ;;
  *) die "unknown bundle mode: ${mode}" ;;
esac

log "check local documentation references"
python3 - "$BUNDLE_DIR" <<'PY'
import pathlib
import re
import sys
from urllib.parse import unquote

root = pathlib.Path(sys.argv[1]).resolve()
documents = [root / "README.md", root / "INSTALL.md", *sorted((root / "docs").glob("*.md"))]
link_re = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
for document in documents:
    text = document.read_text(encoding="utf-8")
    for match in link_re.finditer(text):
        raw = match.group(1).strip()
        if raw.startswith("<") and raw.endswith(">"):
            raw = raw[1:-1]
        if (
            not raw
            or raw.startswith("#")
            or re.match(r"^[a-z][a-z0-9+.-]*:", raw, re.IGNORECASE)
        ):
            continue
        target = unquote(raw.split("#", 1)[0].split("?", 1)[0])
        candidate = (document.parent / target).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            raise SystemExit(
                f"error: {document.relative_to(root)} reference escapes bundle: {raw}"
            )
        if not candidate.exists():
            raise SystemExit(
                f"error: {document.relative_to(root)} has dangling reference: {raw}"
            )
PY

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
export HELM_REPOSITORY_CONFIG="${tmp_dir}/repositories.yaml"
export HELM_REPOSITORY_CACHE="${tmp_dir}/repository-cache"
export HELM_REGISTRY_CONFIG="${tmp_dir}/registry.json"

image_args=(
  --set-string "jeen-insights-api.image.repository=${registry}/${namespace}/jeen-insights-api"
  --set-string "jeen-insights-ui.image.repository=${registry}/${namespace}/jeen-insights-ui"
  --set-string "jeen-insights-analytics.image.repository=${registry}/${namespace}/jeen-insights-analytics"
  --set-string "jeen-insights-api.image.tag=${tag}"
  --set-string "jeen-insights-ui.image.tag=${tag}"
  --set-string "jeen-insights-analytics.image.tag=${tag}"
)

log "lint and render packaged chart offline"
helm lint "${BUNDLE_DIR}/${chart_archive}" \
  --values "${BUNDLE_DIR}/values.openshift.yaml" \
  "${image_args[@]}"
helm template jeen-insights "${BUNDLE_DIR}/${chart_archive}" \
  --namespace "$namespace" \
  --values "${BUNDLE_DIR}/values.openshift.yaml" \
  "${image_args[@]}" \
  > "${tmp_dir}/manifests.yaml"

migration_args=(
  --set-string "migration.bundleId=${migration_bundle_id}"
  --set-string "migration.image.repository=${registry}/${namespace}/jeen-insights-api"
  --set-string "migration.image.tag=${tag}"
  --set-string "migration.existingSecret.name=jeen-insights-secrets"
)
helm template jeen-insights "${BUNDLE_DIR}/${chart_archive}" \
  --namespace "$namespace" \
  --values "${BUNDLE_DIR}/values.openshift.yaml" \
  --values "${BUNDLE_DIR}/values.migration.example.yaml" \
  "${migration_args[@]}" \
  --show-only templates/migration-job.yaml \
  > "${tmp_dir}/migration-job.yaml"

cmp -s "${BUNDLE_DIR}/manifests.yaml" "${tmp_dir}/manifests.yaml" \
  || die "manifests.yaml does not match the packaged chart and bundled values"
cmp -s "${BUNDLE_DIR}/migration-job.yaml" "${tmp_dir}/migration-job.yaml" \
  || die "migration-job.yaml does not match the packaged chart and bundled values"

printf 'Bundle validation passed (%s, chart %s, %s).\n' "$mode" "$chart_version" "$platform"
