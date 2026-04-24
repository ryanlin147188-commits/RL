#!/usr/bin/env bash
# =============================================================================
# Bundle 容器 Entrypoint — 把內建的 AutoTest 專案解壓到使用者掛載目錄，
# 然後（可選）透過 host 的 Docker daemon 執行 deploy.sh。
# =============================================================================
set -euo pipefail

# ── 色碼 ─────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()     { echo -e "${RED}[ERROR]${NC} $*" >&2; }
header()  { echo -e "\n${BOLD}${CYAN}▶ $*${NC}\n"; }

SRC=/opt/autotest     # image 內建的專案源目錄
TARGET_SUBDIR="autotest_v1.0"  # 在使用者當下工作目錄下建立的子資料夾名

print_banner() {
cat <<'EOF'
╔════════════════════════════════════════════════════════════════╗
║                                                                ║
║        AutoTest v1.0  —  Bundle 一鍵部署器                       ║
║                                                                ║
╚════════════════════════════════════════════════════════════════╝
EOF
}

usage() {
    cat <<EOF
${BOLD}AutoTest v1.0 Bundle${NC}

用法：
  docker run --rm -it \\
      -v /var/run/docker.sock:/var/run/docker.sock \\
      -v "\$PWD:\$PWD" -w "\$PWD" \\
      autotest_v1.0 [子命令]

子命令：
  install   (預設) 解壓 + 一鍵部署（需要 docker.sock 與 bind mount）
  extract   只解壓到 ./${TARGET_SUBDIR}/，不執行 deploy
  info      顯示此說明

Entrypoint 會把內建的 AutoTest 專案解壓到 \$PWD/${TARGET_SUBDIR}/
然後透過掛載的 host Docker 執行 deploy.sh。
EOF
}

ensure_target_writable() {
    local dst="$1"
    # dst 必須在 host 與容器內有相同路徑（靠 bind mount `-v \$PWD:\$PWD` 達成）
    mkdir -p "$dst"
    if ! touch "$dst/.write_test" 2>/dev/null; then
        err "目標目錄 $dst 沒有寫入權限。請確認有做 bind mount，例如：-v \"\$PWD:\$PWD\" -w \"\$PWD\""
        exit 1
    fi
    rm -f "$dst/.write_test"
}

copy_project() {
    local dst="$1"
    header "解壓 AutoTest 專案到 $dst"
    # 使用 rsync 精確複製（保留權限 + 排除一些執行時產生物）
    rsync -a \
        --exclude='.git' \
        --exclude='PIC' \
        --exclude='record' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.venv' \
        --exclude='node_modules' \
        "$SRC"/ "$dst"/
    chmod +x "$dst"/deploy.sh 2>/dev/null || true
    success "解壓完成"
}

run_deploy() {
    local dst="$1"
    header "透過 host Docker 執行 deploy.sh"
    if [ ! -S /var/run/docker.sock ]; then
        err "沒有偵測到 /var/run/docker.sock。"
        echo "   必須加上 ${BOLD}-v /var/run/docker.sock:/var/run/docker.sock${NC} 才能使用 install 模式。"
        echo "   若只想解壓檔案，請改用 ${BOLD}extract${NC} 子命令。"
        exit 1
    fi
    cd "$dst"
    ./deploy.sh
}

# ── 主流程 ───────────────────────────────────────────────────────────
CMD="${1:-install}"

case "$CMD" in
    install)
        print_banner
        # PWD 是透過 -w "$PWD" 傳進來的 host 工作目錄（bind mount 已讓容器內外同路徑）
        local_target="$PWD/$TARGET_SUBDIR"
        ensure_target_writable "$local_target"
        copy_project "$local_target"
        run_deploy "$local_target"
        ;;
    extract)
        print_banner
        local_target="$PWD/$TARGET_SUBDIR"
        ensure_target_writable "$local_target"
        copy_project "$local_target"
        info "下一步："
        echo "    cd $TARGET_SUBDIR"
        echo "    ./deploy.sh     # 啟動"
        ;;
    info|help|--help|-h)
        print_banner
        usage
        ;;
    bash|sh)
        # 除錯用：進入容器 shell
        exec /bin/bash
        ;;
    *)
        err "未知子命令：$CMD"
        usage
        exit 1
        ;;
esac
