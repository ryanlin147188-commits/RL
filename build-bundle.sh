#!/usr/bin/env bash
# =============================================================================
# build-bundle.sh — 一鍵打包 AutoTest 為單一 Docker image
#
# 產出：`autotest_v1.0:latest` image（Docker 不允許 tag 包含大寫；以 tag v1.0 並行）
#
# 用法：
#   ./build-bundle.sh         # 建置 bundle image
#   ./build-bundle.sh --run   # 建置完並直接啟動（掛 sock + PWD 同路徑）
#   ./build-bundle.sh --save  # 建置完另存成 .tar 以便離線散佈
# =============================================================================
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()     { echo -e "${RED}[ERROR]${NC} $*" >&2; }
header()  { echo -e "\n${BOLD}${CYAN}▶ $*${NC}\n"; }

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

IMAGE_REPO="autotest_v1.0"
IMAGE_TAG="latest"
IMAGE_TAG_V="v1.0"
IMAGE="$IMAGE_REPO:$IMAGE_TAG"
IMAGE_V="$IMAGE_REPO:$IMAGE_TAG_V"
TAR_FILE="autotest_v1.0-bundle.tar"

# ── 檢查 Docker ──────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || { err "找不到 docker 指令"; exit 1; }
docker info >/dev/null 2>&1 || { err "Docker daemon 沒在跑，請啟動 Docker Desktop"; exit 1; }

# ── Build ────────────────────────────────────────────────────────────
header "Build $IMAGE（以 Ubuntu 24.04 為基底，打包整個專案）"
docker build -f Dockerfile.bundle -t "$IMAGE" -t "$IMAGE_V" .
success "已建立：$IMAGE 與 $IMAGE_V"

# ── 顯示 image 大小 ──────────────────────────────────────────────────
size=$(docker images --format '{{.Size}}' "$IMAGE" | head -1)
info "Image 大小：$size"

# ── 選項處理 ─────────────────────────────────────────────────────────
for arg in "$@"; do
    case "$arg" in
        --save|save)
            header "匯出 image 為 $TAR_FILE（離線散佈用）"
            docker save -o "$TAR_FILE" "$IMAGE"
            success "已輸出：$TAR_FILE ($(du -h "$TAR_FILE" | cut -f1))"
            info "在目標機器載入：docker load -i $TAR_FILE"
            ;;
        --run|run)
            header "啟動 bundle（掛 host docker.sock 與工作目錄）"
            docker run --rm -it \
                -v /var/run/docker.sock:/var/run/docker.sock \
                -v "$PWD:$PWD" -w "$PWD" \
                "$IMAGE" install
            ;;
    esac
done

# ── 印出使用方式 ─────────────────────────────────────────────────────
cat <<EOF

${GREEN}${BOLD}══════════════════════════════════════════════════════════${NC}
${GREEN}${BOLD}  AutoTest v1.0 Bundle 已建立完成${NC}
${GREEN}${BOLD}══════════════════════════════════════════════════════════${NC}

  ${BOLD}一鍵部署到新機器${NC}：

    docker run --rm -it \\
        -v /var/run/docker.sock:/var/run/docker.sock \\
        -v "\$PWD:\$PWD" -w "\$PWD" \\
        $IMAGE

  ${BOLD}只解壓不部署${NC}：

    docker run --rm -v "\$PWD:\$PWD" -w "\$PWD" $IMAGE extract

  ${BOLD}離線散佈${NC}：

    ./build-bundle.sh --save    # 產出 $TAR_FILE
    # 在另一台機器：
    docker load -i $TAR_FILE

EOF
