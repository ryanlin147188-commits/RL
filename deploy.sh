#!/usr/bin/env bash
# =============================================================================
# AutoTest 一鍵部署腳本 (macOS / Linux / Ubuntu)
# -----------------------------------------------------------------------------
#   用法：   ./deploy.sh          部署並啟動全部服務
#            ./deploy.sh --stop   停止但保留資料
#            ./deploy.sh --reset  停止並清空所有資料（破壞性，需再打 y 確認）
#            ./deploy.sh --logs   即時跟著 compose logs
#            ./deploy.sh --status 顯示容器狀態
# =============================================================================
set -euo pipefail

# ── 色碼 ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1" >&2; }
step()    { echo -e "\n${CYAN}${BOLD}▶ $1${NC}"; }

# ── 進入腳本所在目錄（允許從任何位置執行）──────────────────────────
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# ── 子命令 ───────────────────────────────────────────────────────────
CMD="${1:-deploy}"

# ── 檢查 Docker ──────────────────────────────────────────────────────
require_docker() {
    step "檢查 Docker 環境"
    if ! command -v docker >/dev/null 2>&1; then
        error "找不到 docker 指令。請先安裝 Docker：https://docs.docker.com/get-docker/"
        exit 1
    fi
    if ! docker info >/dev/null 2>&1; then
        error "Docker daemon 沒在跑。"
        if [[ "$OSTYPE" == "darwin"* ]]; then
            echo "   macOS：請開啟 Docker Desktop"
        else
            echo "   Linux：sudo systemctl start docker"
        fi
        exit 1
    fi
    if ! docker compose version >/dev/null 2>&1; then
        error "找不到 docker compose v2。請更新 Docker Desktop 或安裝 docker-compose-plugin。"
        exit 1
    fi
    success "Docker $(docker --version | awk '{print $3}' | tr -d ',') / Compose $(docker compose version --short)"
}

# ── 檢查專案目錄 ─────────────────────────────────────────────────────
check_project_dir() {
    [[ -f docker-compose.yml ]] || { error "找不到 docker-compose.yml；請確認腳本放在專案根目錄。"; exit 1; }
    [[ -d backend ]] || { error "找不到 backend/ 資料夾。"; exit 1; }
}

# ── 建立 .env（若不存在） ────────────────────────────────────────────
ensure_env() {
    if [[ ! -f .env ]]; then
        info "未偵測到 .env，以預設值建立..."
        cat > .env <<'EOF'
# AutoTest 部署設定 — PostgreSQL / Valkey / SeaweedFS 預設帳密 admin / admin123
DB_USER=admin
DB_PASSWORD=admin123
DB_NAME=autotest_db
BASE_URL=http://localhost
STORAGE_BACKEND=minio
MINIO_ROOT_USER=admin
MINIO_ROOT_PASSWORD=admin123
EOF
        success ".env 已建立（admin / admin123）"
    else
        info ".env 已存在，沿用既有設定"
    fi
}

# ── 建 Robot Runner 容器 image ───────────────────────────────────────
build_runner_image() {
    if docker image inspect autotest-robot-runner:latest >/dev/null 2>&1; then
        info "autotest-robot-runner 已存在（跳過重建；需更新請先 docker rmi autotest-robot-runner:latest）"
        return
    fi
    info "建 Robot Runner 容器 image（第一次約 3-5 分鐘）..."
    docker build -f backend/Dockerfile.runner -t autotest-robot-runner:latest backend/ \
        || { error "Runner image 建置失敗。"; exit 1; }
    success "Runner image 已建置"
}

# ── 建 Docker 模式錄製 image（autotest-recorder） ────────────────────
# Phase 1:容器內跑 Xvfb + noVNC + Playwright codegen,讓使用者透過瀏覽器
# iframe 在伺服器側錄製,免裝 Playwright。
build_recorder_image() {
    if docker image inspect autotest-recorder:latest >/dev/null 2>&1; then
        info "autotest-recorder 已存在（跳過重建；需更新請先 docker rmi autotest-recorder:latest）"
        return
    fi
    info "建 Recorder 容器 image（含 noVNC + Playwright，第一次約 3-5 分鐘）..."
    docker build -f backend/Dockerfile.recorder -t autotest-recorder:latest backend/ \
        || { error "Recorder image 建置失敗。"; exit 1; }
    success "Recorder image 已建置"
}

# ── 啟動 Compose ─────────────────────────────────────────────────────
compose_up() {
    step "啟動服務（docker compose up -d --build）"
    docker compose up -d --build
    success "Compose 已啟動"
}

# ── 等待 frontend 可訪問 ─────────────────────────────────────────────
wait_for_ready() {
    step "等待服務就緒"
    local max_wait=120 waited=0
    while (( waited < max_wait )); do
        if curl -fsS -o /dev/null http://localhost/ 2>/dev/null; then
            success "前端可訪問 <http://localhost/>"
            return 0
        fi
        sleep 2; waited=$((waited + 2))
        printf "."
    done
    echo
    warn "等待 ${max_wait}s 後仍沒回應，請檢查 'docker compose logs frontend backend'。"
}

# ── 確保 PostgreSQL admin 使用者存在 ────────────────────────────────
# PostgreSQL 的 POSTGRES_USER / POSTGRES_PASSWORD 會在首次啟動建立 superuser，
# 故此函式僅作健康檢查；既有 volume（從 MySQL 遷移）需先 down -v 再重來。
ensure_postgres_admin() {
    step "驗證 PostgreSQL 連線（admin / admin123）"
    if docker exec autotest-postgres pg_isready -U admin -d "${DB_NAME:-autotest_db}" >/dev/null 2>&1; then
        success "PostgreSQL 接受連線"
    else
        warn "尚無法連 PostgreSQL；若仍在啟動中可忽略，或檢查 docker logs autotest-postgres"
    fi
}

# ── 開啟瀏覽器（macOS / Linux 自動偵測） ──────────────────────────
open_browser() {
    local url="http://localhost/"
    if command -v open >/dev/null 2>&1; then open "$url" 2>/dev/null || true        # macOS
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$url" 2>/dev/null || true  # Linux
    fi
}

# ── 印出橫幅 ──────────────────────────────────────────────────────────
print_banner() {
    local apple_note=""
    if [[ "$OSTYPE" == "darwin"* ]] && [[ "$(uname -m)" == "arm64" ]]; then
        apple_note="\n  ${YELLOW}🍎 Apple Silicon 提醒：Runner 容器為 amd64，啟動會比原生慢 2-4 倍。${NC}"
    fi
    cat <<EOF

${GREEN}${BOLD}════════════════════════════════════════════════════════════${NC}
${GREEN}${BOLD}    ✨  AutoTest 部署完成！ ✨${NC}
${GREEN}${BOLD}════════════════════════════════════════════════════════════${NC}

  🌐 開啟： ${CYAN}http://localhost/${NC}
  🔑 登入： ${CYAN}admin / admin123${NC}
  📘 API ： ${CYAN}http://localhost:8000/docs${NC}  (Swagger UI)
  🗄  SeaweedFS Master：${CYAN}http://localhost:9333/${NC} (status / cluster info)
  🗄  SeaweedFS S3 API：${CYAN}http://localhost:8333/${NC}  (admin / admin123)
$(echo -e "$apple_note")
  常用指令：
    ./deploy.sh --status   看容器狀態
    ./deploy.sh --logs     看即時 log
    ./deploy.sh --stop     停掉服務（保留資料）
    ./deploy.sh --reset    停掉並清空所有資料（破壞性）

EOF
}

# ── 子命令實作 ───────────────────────────────────────────────────────
case "$CMD" in
    deploy|"")
        require_docker
        check_project_dir
        ensure_env
        build_runner_image
        build_recorder_image
        compose_up
        wait_for_ready
        ensure_postgres_admin
        print_banner
        open_browser
        ;;
    --stop|stop)
        require_docker; check_project_dir
        info "停止服務..."; docker compose down
        success "已停止（資料保留在 volumes 裡，下次 deploy 可直接恢復）"
        ;;
    --reset|reset)
        require_docker; check_project_dir
        warn "此動作會刪除所有 PostgreSQL / SeaweedFS volumes，測試資料、報告、截圖都會消失！"
        read -p "確定要繼續嗎？[y/N] " yn
        [[ "$yn" =~ ^[Yy]$ ]] || { info "已取消。"; exit 0; }
        docker compose down -v
        success "已重置（下次 deploy 會以全新狀態啟動）"
        ;;
    --logs|logs)
        require_docker; check_project_dir
        docker compose logs -f
        ;;
    --status|status|ps)
        require_docker; check_project_dir
        docker compose ps
        ;;
    -h|--help|help)
        sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    *)
        error "未知子命令：$CMD"
        echo "  用法：./deploy.sh [--stop|--reset|--logs|--status|--help]"
        exit 1
        ;;
esac
