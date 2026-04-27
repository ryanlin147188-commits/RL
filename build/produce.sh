#!/bin/bash
# 一鍵 build 全部 4 個生產 image。
#
# 用法:
#   ./build/produce.sh [TAG] [REGISTRY_PREFIX]
# 範例:
#   ./build/produce.sh 1.0.0
#   ./build/produce.sh 1.0.0 myregistry.example.com/autotest/
#
# 完成後可:
#   docker push 各個 ${REGISTRY_PREFIX}autotest-*:${TAG}
# 或:
#   docker compose -f build/docker-compose.production.yml up -d
set -euo pipefail

# 進到 repo root(腳本在 build/ 內,build context 必須是 repo root)
cd "$(dirname "${BASH_SOURCE[0]}")/.."

TAG="${1:-1.0.0}"
REGISTRY="${2:-}"

echo "==> Building autotest-* images, tag=${TAG}, registry=${REGISTRY:-<local>}"

build_one() {
    local name="$1"
    local dockerfile="$2"
    local full="${REGISTRY}autotest-${name}:${TAG}"
    echo "─── ${name} → ${full} ───"
    docker build -f "${dockerfile}" -t "${full}" .
    echo
}

build_one backend  build/Dockerfile.backend.production
build_one celery   build/Dockerfile.celery.production
build_one runner   build/Dockerfile.runner.production
build_one frontend build/Dockerfile.frontend.production

echo "==> All images built. Summary:"
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}' | grep -E "^${REGISTRY}autotest-(backend|celery|runner|frontend)\s" | sort

echo
echo "==> Next steps:"
if [ -n "${REGISTRY}" ]; then
    echo "    Push images to registry:"
    echo "      docker push ${REGISTRY}autotest-backend:${TAG}"
    echo "      docker push ${REGISTRY}autotest-celery:${TAG}"
    echo "      docker push ${REGISTRY}autotest-runner:${TAG}"
    echo "      docker push ${REGISTRY}autotest-frontend:${TAG}"
    echo
    echo "    Customer 端執行:"
    echo "      export AUTOTEST_TAG=${TAG} REGISTRY=${REGISTRY}"
    echo "      docker compose -f build/docker-compose.production.yml pull"
    echo "      docker compose -f build/docker-compose.production.yml up -d"
else
    echo "    Local smoke test:"
    echo "      AUTOTEST_TAG=${TAG} docker compose -f build/docker-compose.production.yml up -d"
fi
