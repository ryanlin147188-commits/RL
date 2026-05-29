#!/bin/sh
# 啟動 celery worker。
# - 預設 headless（PLAYWRIGHT_HEADLESS=1），不需要 X server。
# - 若使用者把 PLAYWRIGHT_HEADLESS 設為 0/false 才嘗試拉 Xvfb 提供假顯示；
#   若 Xvfb 不可用就清掉 DISPLAY，讓 Chromium 直接報錯而不是被誤導。
set -e

case "${PLAYWRIGHT_HEADLESS:-1}" in
    0|false|False|FALSE)
        NEED_XVFB=1
        ;;
    *)
        NEED_XVFB=0
        ;;
esac

if [ "$NEED_XVFB" = "1" ] && command -v Xvfb >/dev/null 2>&1; then
    DISPLAY_NUM=99
    export DISPLAY=":${DISPLAY_NUM}"

    if ! pgrep -x Xvfb >/dev/null 2>&1; then
        Xvfb "${DISPLAY}" -screen 0 1280x720x24 -nolisten tcp -nocursor &
        XVFB_PID=$!
        # 等 X socket 就緒（最多 5 秒）
        XVFB_READY=0
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            if [ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
                XVFB_READY=1
                break
            fi
            sleep 0.5
        done
        if [ "$XVFB_READY" != "1" ]; then
            echo "[entrypoint] WARNING: Xvfb 未就緒，改以無 DISPLAY 執行" >&2
            unset DISPLAY
        fi
    fi

    trap 'if [ -n "${XVFB_PID:-}" ]; then kill "$XVFB_PID" 2>/dev/null || true; fi' TERM INT EXIT
fi

# Phase 6.3:同進程啟動 beat scheduler(``-B``),讓 Casdoor 5 分鐘 reconcile
# 自動跑;``-s /tmp/celerybeat-schedule`` 把 schedule state 寫在容器 tmpfs,
# 重啟即重置(無狀態 beat,我們的 schedule 都是純秒數間隔,不依賴 last-run)。
#
# CELERY_CONCURRENCY:prefork pool 大小。每個 slot 同時可跑一個 run_tests,
# 該 task 內會 spawn 一個 robot-runner container(平均 1-2GB 記憶體)。預設
# 4 — 對中型 VM 適合;若 VM 資源緊張可在 .env 改 2,寬鬆改 8。原本寫死 2,
# 任何長 test 都會把整個 worker pool 卡住,後續 task 全在 valkey queue 排隊。
exec celery -A tasks.celery_app worker --loglevel=info \
    --concurrency="${CELERY_CONCURRENCY:-4}" \
    -B -s /tmp/celerybeat-schedule
