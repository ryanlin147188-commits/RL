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

# ── 隨機 secret 產生器 ───────────────────────────────────────────────
# rand_hex N — 產生 N bytes 的 hex 字串(預設 32 bytes / 64 chars)
rand_hex() {
    local bytes="${1:-32}"
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex "$bytes"
    else
        # 退路:用 /dev/urandom + xxd / od
        if command -v xxd >/dev/null 2>&1; then
            head -c "$bytes" /dev/urandom | xxd -p | tr -d '\n'
        else
            head -c "$bytes" /dev/urandom | od -An -tx1 | tr -d ' \n'
        fi
    fi
}

# rand_fernet — 產生 Fernet key(32 random bytes 的 url-safe base64)
rand_fernet() {
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
    elif command -v python >/dev/null 2>&1; then
        python -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
    else
        # 退路:用 openssl 產 base64,再轉成 url-safe(replace '+' '-' / '/' '_')
        openssl rand 32 | base64 | tr '+/' '-_' | tr -d '\n='
    fi
}

# ── 建立 / 補齊 .env ─────────────────────────────────────────────────
# - .env 不存在:用隨機 secret 寫一份完整的
# - .env 已存在:檢查必要 key 是否齊全;缺的 key 隨機產生並 append(舊值不動)
ensure_env() {
    local f=.env
    if [[ ! -f $f ]]; then
        info "未偵測到 .env,自動產生隨機密碼/secret..."
        cat > $f <<EOF
# AutoTest 部署設定(自動產生於 $(date '+%Y-%m-%d %H:%M:%S'))
# 此檔內含隨機密碼,請妥善保管,切勿 commit 進 git。
DB_USER=admin
DB_PASSWORD=$(rand_hex 24)
DB_NAME=autotest_db
BASE_URL=http://localhost
ALLOWED_ORIGINS=http://localhost
STORAGE_BACKEND=s3
S3_ROOT_USER=admin
S3_ROOT_PASSWORD=$(rand_hex 24)
AUTOTEST_JWT_SECRET=$(rand_hex 32)
AUTOTEST_FERNET_KEY=$(rand_fernet)
EOF
        success ".env 已建立(全部 secret 隨機產生)"
        return
    fi
    info ".env 已存在,檢查必要欄位..."
    local appended=0
    ensure_env_var "$f" DB_USER admin && appended=$((appended+1))
    ensure_env_var "$f" DB_PASSWORD "$(rand_hex 24)" && appended=$((appended+1))
    ensure_env_var "$f" DB_NAME autotest_db && appended=$((appended+1))
    ensure_env_var "$f" BASE_URL http://localhost && appended=$((appended+1))
    ensure_env_var "$f" ALLOWED_ORIGINS http://localhost && appended=$((appended+1))
    ensure_env_var "$f" STORAGE_BACKEND s3 && appended=$((appended+1))
    ensure_env_var "$f" S3_ROOT_USER admin && appended=$((appended+1))
    ensure_env_var "$f" S3_ROOT_PASSWORD "$(rand_hex 24)" && appended=$((appended+1))
    ensure_env_var "$f" AUTOTEST_JWT_SECRET "$(rand_hex 32)" && appended=$((appended+1))
    ensure_env_var "$f" AUTOTEST_FERNET_KEY "$(rand_fernet)" && appended=$((appended+1))
    if (( appended == 0 )); then
        success ".env 完整"
    else
        warn ".env 已附加 ${appended} 個缺少的 key(舊值未變動)"
    fi
}

# ensure_env_var FILE KEY DEFAULT_VALUE
# 若 FILE 中沒有 KEY=...,append KEY=DEFAULT_VALUE 並 return 0(代表有附加);否則 return 1
ensure_env_var() {
    local file=$1 key=$2 value=$3
    if grep -qE "^${key}=" "$file"; then
        return 1
    fi
    echo "${key}=${value}" >> "$file"
    return 0
}

# ── 建 Robot Runner 容器 image ───────────────────────────────────────
build_runner_image() {
    if docker image inspect autotest-robot-runner:1.0.0 >/dev/null 2>&1; then
        info "autotest-robot-runner 已存在（跳過重建；需更新請先 docker rmi autotest-robot-runner:1.0.0）"
        return
    fi
    info "建 Robot Runner 容器 image（第一次約 3-5 分鐘）..."
    docker build -f backend/Dockerfile.runner -t autotest-robot-runner:1.0.0 backend/ \
        || { error "Runner image 建置失敗。"; exit 1; }
    success "Runner image 已建置"
}

# ── 建 Docker 模式錄製 image（autotest-recorder） ────────────────────
# Phase 1:容器內跑 Xvfb + noVNC + Playwright codegen,讓使用者透過瀏覽器
# iframe 在伺服器側錄製,免裝 Playwright。
build_recorder_image() {
    if docker image inspect autotest-recorder:1.0.0 >/dev/null 2>&1; then
        info "autotest-recorder 已存在（跳過重建；需更新請先 docker rmi autotest-recorder:1.0.0）"
        return
    fi
    info "建 Recorder 容器 image（含 noVNC + Playwright，第一次約 3-5 分鐘）..."
    docker build -f backend/Dockerfile.recorder -t autotest-recorder:1.0.0 backend/ \
        || { error "Recorder image 建置失敗。"; exit 1; }
    success "Recorder image 已建置"
}

# ── 建 API 模式錄製 image (autotest-recorder-api：mitmproxy + HAR addon) ────
build_recorder_api_image() {
    if docker image inspect autotest-recorder-api:1.0.0 >/dev/null 2>&1; then
        info "autotest-recorder-api 已存在（跳過重建；需更新請先 docker rmi autotest-recorder-api:1.0.0）"
        return
    fi
    info "建 Recorder-API 容器 image (mitmproxy；第一次約 1-2 分鐘)..."
    docker build -f backend/Dockerfile.recorder-api -t autotest-recorder-api:1.0.0 backend/ \
        || { error "Recorder-API image 建置失敗。"; exit 1; }
    success "Recorder-API image 已建置"
}

# ── 建 MCP image (autotest-mcp:Playwright MCP server) ──────────────
# Sprint 4.3 PoC:讓 LLM 透過 tool calling 直接控制 chromium。
# 第一次 build 會抓 npm @playwright/mcp,約 2-3 分鐘。
build_mcp_image() {
    if docker image inspect autotest-mcp:1.0.0 >/dev/null 2>&1; then
        info "autotest-mcp 已存在（跳過重建;需更新請先 docker rmi autotest-mcp:1.0.0）"
        return
    fi
    info "建 MCP 容器 image (Playwright MCP;第一次約 2-3 分鐘)..."
    docker build -f backend/Dockerfile.mcp -t autotest-mcp:1.0.0 backend/ \
        || { error "MCP image 建置失敗。"; exit 1; }
    success "MCP image 已建置"
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

# ── 確保 PostgreSQL 接受連線 ────────────────────────────────────────
# POSTGRES_USER / POSTGRES_PASSWORD 在 image 首次啟動時建立(volume 已存在則沿用舊值)。
ensure_postgres_admin() {
    # 載入 .env 取得 DB_USER / DB_NAME(密碼不需要,只做 pg_isready 不用密碼)
    local db_user="admin" db_name="autotest_db"
    if [[ -f .env ]]; then
        db_user=$(grep -E '^DB_USER=' .env | head -1 | cut -d= -f2-)
        db_name=$(grep -E '^DB_NAME=' .env | head -1 | cut -d= -f2-)
        db_user="${db_user:-admin}"
        db_name="${db_name:-autotest_db}"
    fi
    step "驗證 PostgreSQL 連線(${db_user} / ${db_name})"
    if docker exec autotest-postgres pg_isready -U "${db_user}" -d "${db_name}" >/dev/null 2>&1; then
        success "PostgreSQL 接受連線"
    else
        warn "尚無法連 PostgreSQL;若仍在啟動中可忽略,或檢查 docker logs autotest-postgres"
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
${GREEN}${BOLD}    ✨  AutoTest 部署完成! ✨${NC}
${GREEN}${BOLD}════════════════════════════════════════════════════════════${NC}

  🌐 前端  : ${CYAN}http://localhost/${NC}
  📘 API  : ${CYAN}http://localhost:8000/docs${NC}  (Swagger UI)
  🗄  SeaweedFS S3 API: ${CYAN}http://localhost:8333/${NC}  (帳密見 .env)
  📊 日誌  : ${CYAN}http://localhost:9428/select/vmui/${NC}  (VictoriaLogs)

  ${YELLOW}首次部署:資料庫沒有任何使用者,請執行以下指令建立 admin:${NC}
    ${CYAN}docker compose exec backend python -m app.cli create-admin${NC}
$(echo -e "$apple_note")
  常用指令:
    ./deploy.sh --status   看容器狀態
    ./deploy.sh --logs     看即時 log
    ./deploy.sh --stop     停掉服務(保留資料)
    ./deploy.sh --reset    停掉並清空所有資料(破壞性)

  資料庫 / S3 密碼存放在 ${CYAN}.env${NC}(已加入 .gitignore;切勿 commit)。

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
        build_recorder_api_image
        build_mcp_image
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
