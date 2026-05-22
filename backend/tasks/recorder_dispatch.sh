#!/bin/bash
# Recorder 統一 image dispatcher。
#
# v1.1.9 起,原 autotest-recorder / autotest-recorder-api / autotest-mcp
# 三個 image 合併成一份 autotest-recorder。透過 RECORDER_MODE env 切換
# 實際入口:
#
#   RECORDER_MODE=novnc      → Xvfb + noVNC + Playwright codegen(預設)
#   RECORDER_MODE=mitmweb    → mitmweb 8080 proxy + 8081 web UI + HAR addon
#   RECORDER_MODE=mcp        → @playwright/mcp SSE server on 8931
#
# backend spawn 容器時用 environment 注入 RECORDER_MODE;沒設預設 novnc。
set -e

MODE="${RECORDER_MODE:-novnc}"

case "$MODE" in
    novnc)
        exec /usr/local/bin/recorder-entrypoint.sh "$@"
        ;;
    mitmweb)
        exec /usr/local/bin/recorder-api-entrypoint.sh "$@"
        ;;
    mcp)
        # Playwright MCP server(取代原 autotest-mcp image)。
        # --allowed-hosts '*':backend 走 docker 內部 DNS 連進來,name 不在
        # MCP 預設 allowed list,要放開;容器只跑在 internal network 不對外
        # 曝露,放開是安全的。
        exec npx -y "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION:-0.0.69}" \
             --port 8931 --host 0.0.0.0 --allowed-hosts '*'
        ;;
    *)
        echo "[recorder-dispatch] Unknown RECORDER_MODE='$MODE' (expect: novnc|mitmweb|mcp)" >&2
        exit 2
        ;;
esac
