#!/usr/bin/env bash
# Build a linux/amd64 offline bundle that mirrors dist/RL_TMP_<DATE>_linux.tar.
#
# Output: dist/RL_TMP_<DATE>_linux_amd64.tar containing:
#   RL_TMP_<DATE>_linux_amd64/
#     docker-images.tar.gz         (docker save | gzip, all amd64)
#     docker-images.list           (image:tag, one per line)
#     SHA256SUMS
#     (no source — image-only bundle; for full source bundle, untar the
#      arm64 dist tar and replace docker-images.tar.gz with this one's)

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DATE_TAG="${DATE_TAG:-$(date +%Y%m%d)}"
AUTOTEST_TAG="${AUTOTEST_TAG:-1.1.1}"
HERMES_TAG="${HERMES_TAG:-0.13.0}"
MEM0_TAG="${MEM0_TAG:-0.1.0}"
BUNDLE_NAME="RL_TMP_${DATE_TAG}_linux_amd64"
OUT_DIR="dist/${BUNDLE_NAME}"
OUT_TAR="dist/${BUNDLE_NAME}.tar"
PLATFORM="linux/amd64"

log() { printf '[amd64-bundle] %s\n' "$*"; }

mkdir -p "$OUT_DIR"

# Build matrix: <image:tag> <context> <dockerfile-relative-to-context>
IN_HOUSE=(
    "autotest-backend:${AUTOTEST_TAG}|./backend|Dockerfile"
    "autotest-celery:${AUTOTEST_TAG}|./backend|Dockerfile.celery"
    "autotest-mcp:${AUTOTEST_TAG}|./backend|Dockerfile.mcp"
    "autotest-recorder:${AUTOTEST_TAG}|./backend|Dockerfile.recorder"
    "autotest-recorder-api:${AUTOTEST_TAG}|./backend|Dockerfile.recorder-api"
    "autotest-robot-runner:${AUTOTEST_TAG}|./backend|Dockerfile.runner"
    "autotest-frontend:${AUTOTEST_TAG}|.|frontend/Dockerfile"
    "autotest-hermes:${HERMES_TAG}|./hermes|Dockerfile"
    "autotest-mem0:${MEM0_TAG}|./mem0|Dockerfile"
)

THIRD_PARTY=(
    "alpine:3.20"
    "amazon/aws-cli:2.18.5"
    "apache/apisix:3.11.0-debian"
    "chrislusf/seaweedfs:3.80"
    "fluent/fluent-bit:3.2"
    "jaegertracing/all-in-one:1.62.0"
    "pgvector/pgvector:pg16"
    "postgres:16-alpine"
    "prom/prometheus:v2.55.1"
    "tecnativa/docker-socket-proxy:0.3.0"
    "valkey/valkey:8-alpine"
    "victoriametrics/victoria-logs:v1.50.0"
)

ALL_TAGS=()

# ── 1) Pull third-party images explicitly for amd64 ───────────────────────
log "step 1/4: pulling ${#THIRD_PARTY[@]} third-party images for ${PLATFORM}"
for img in "${THIRD_PARTY[@]}"; do
    log "  pull --platform=${PLATFORM} ${img}"
    docker pull --platform="${PLATFORM}" "${img}"
    ALL_TAGS+=("${img}")
done

# ── 2) Build in-house images for amd64 (buildx --load) ────────────────────
log "step 2/4: building ${#IN_HOUSE[@]} in-house images via buildx (emulated)"
for entry in "${IN_HOUSE[@]}"; do
    IFS='|' read -r tag ctx dockerfile <<<"$entry"
    log "  buildx ${tag}  ctx=${ctx}  -f ${dockerfile}"
    docker buildx build \
        --platform="${PLATFORM}" \
        --load \
        --provenance=false \
        -t "${tag}" \
        -f "${ctx}/${dockerfile}" \
        "${ctx}"
    ALL_TAGS+=("${tag}")
done

# ── 3) docker save | gzip ─────────────────────────────────────────────────
log "step 3/4: docker save ${#ALL_TAGS[@]} images -> ${OUT_DIR}/docker-images.tar.gz"
docker save --platform="${PLATFORM}" "${ALL_TAGS[@]}" | gzip > "${OUT_DIR}/docker-images.tar.gz"

# ── 4) Metadata + SHA256SUMS ──────────────────────────────────────────────
log "step 4/4: writing docker-images.list + SHA256SUMS"
printf '%s\n' "${ALL_TAGS[@]}" | sort > "${OUT_DIR}/docker-images.list"

(
    cd "${OUT_DIR}"
    shasum -a 256 docker-images.tar.gz docker-images.list > SHA256SUMS
)

log "wrapping into ${OUT_TAR}"
tar -cf "${OUT_TAR}" -C dist "${BUNDLE_NAME}"

log "done"
ls -lh "${OUT_TAR}" "${OUT_DIR}/docker-images.tar.gz"
