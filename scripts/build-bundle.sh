#!/usr/bin/env bash
# AutoTest v1.0 -- offline bundle builder (RFC-13).
#
# Produces a tarball customers can `docker load` and run via the single
# compose file with `docker compose up -d --no-build`. Each release ships:
#
#   bundle/
#     autotest-images-<VERSION>.tar.gz      (docker save | gzip)
#     manifest.json                          version + image digests
#     SHA256SUMS                             verifiable by verify-bundle.sh
#     sbom/<image>.spdx.json                 syft SPDX SBOM per image (if syft installed)
#     signatures/<image>.sig                 cosign signatures per image (if cosign + key)
#
# Required env (set or take defaults):
#     IMAGE_PREFIX   namespace prefix, e.g. ghcr.io/your-org   (default: autotest)
#     VERSION        semver tag, defaults to `git describe --tags --always`
#     COSIGN_KEY     path to cosign private key; if unset, signing is skipped
#     SBOM           "1" to attempt SBOM via syft (skips silently if syft missing)
#
# Designed to run inside CI but also works locally.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

IMAGE_PREFIX="${IMAGE_PREFIX:-autotest}"
VERSION="${VERSION:-$(git describe --tags --always 2>/dev/null || echo 0.0.0-dev)}"
BUNDLE_DIR="${BUNDLE_DIR:-./bundle}"

IMAGES=(
    autotest-backend
    autotest-celery
    autotest-frontend
    autotest-recorder
    autotest-recorder-api
    autotest-robot-runner
    autotest-mcp
)

mkdir -p "$BUNDLE_DIR/sbom" "$BUNDLE_DIR/signatures"

echo "[bundle] version=$VERSION prefix=$IMAGE_PREFIX"

# ── 1) Confirm all images exist locally ───────────────────────────────────
TAGGED=()
for short in "${IMAGES[@]}"; do
    full="$IMAGE_PREFIX/$short:$VERSION"
    if ! docker image inspect "$full" >/dev/null 2>&1; then
        # Fallback: maybe the image is built with the legacy autotest-* tag.
        legacy="$short:$VERSION"
        if docker image inspect "$legacy" >/dev/null 2>&1; then
            docker tag "$legacy" "$full"
        else
            echo "[bundle] ERROR: image not found locally: $full (or $legacy)" >&2
            exit 1
        fi
    fi
    TAGGED+=("$full")
done

# ── 2) Optional SBOM (syft) ───────────────────────────────────────────────
if [ "${SBOM:-0}" = "1" ] && command -v syft >/dev/null 2>&1; then
    for full in "${TAGGED[@]}"; do
        short="${full##*/}"; short="${short%%:*}"
        echo "[bundle] sbom $full ..."
        syft "$full" -o spdx-json > "$BUNDLE_DIR/sbom/${short}.spdx.json"
    done
else
    echo "[bundle] skipping SBOM (set SBOM=1 and install syft to enable)"
fi

# ── 3) Optional cosign signing ────────────────────────────────────────────
if [ -n "${COSIGN_KEY:-}" ] && command -v cosign >/dev/null 2>&1; then
    for full in "${TAGGED[@]}"; do
        short="${full##*/}"; short="${short%%:*}"
        echo "[bundle] cosign sign $full ..."
        cosign sign --yes --key "$COSIGN_KEY" "$full"
        cosign generate-key-pair >/dev/null 2>&1 || true  # no-op if exists
    done
else
    echo "[bundle] skipping cosign signing (set COSIGN_KEY and install cosign)"
fi

# ── 4) docker save | gzip ─────────────────────────────────────────────────
TARBALL="$BUNDLE_DIR/autotest-images-$VERSION.tar.gz"
echo "[bundle] docker save -> $TARBALL"
docker save "${TAGGED[@]}" | gzip > "$TARBALL"

# ── 5) Manifest ───────────────────────────────────────────────────────────
manifest_images_json=""
for full in "${TAGGED[@]}"; do
    short="${full##*/}"; short="${short%%:*}"
    digest="$(docker inspect --format '{{.Id}}' "$full")"
    if [ -n "$manifest_images_json" ]; then
        manifest_images_json+=","
    fi
    manifest_images_json+="$(printf '{"name":"%s","tag":"%s","digest":"%s"}' "$short" "$full" "$digest")"
done

cat > "$BUNDLE_DIR/manifest.json" <<EOF
{
  "schema": "autotest-bundle/v1",
  "version": "$VERSION",
  "git_rev": "$(git rev-parse --short HEAD 2>/dev/null || echo unknown)",
  "image_prefix": "$IMAGE_PREFIX",
  "tarball": "$(basename "$TARBALL")",
  "images": [$manifest_images_json]
}
EOF

# ── 6) SHA256SUMS over the user-facing artefacts ──────────────────────────
(
    cd "$BUNDLE_DIR"
    files=(autotest-images-"$VERSION".tar.gz manifest.json)
    [ -d sbom ] && files+=(sbom/*.spdx.json) 2>/dev/null || true
    sha256sum -- "${files[@]}" 2>/dev/null > SHA256SUMS
)

echo "[bundle] integrity:"
cat "$BUNDLE_DIR/SHA256SUMS"
echo "[bundle] done -> $BUNDLE_DIR"
