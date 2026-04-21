#!/bin/sh
# 啟動 celery worker；headed Playwright 需要可用的 $DISPLAY，所以先拉一個
# Xvfb 虛擬顯示。xvfb-run 的 SIGUSR1 ready-signal 在 Docker/PID1 情境下有時
# 會卡死，因此這裡改以明確手動啟動 + 輪詢 socket 的方式。
set -e

DISPLAY_NUM=99
export DISPLAY=":${DISPLAY_NUM}"

# 啟動 Xvfb（背景）。若已在執行則沿用
if ! pgrep -x Xvfb >/dev/null 2>&1; then
    Xvfb "${DISPLAY}" -screen 0 1280x720x24 -nolisten tcp -nocursor &
    XVFB_PID=$!
    # 等 X socket 就緒（最多 5 秒）
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        [ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ] && break
        sleep 0.5
    done
fi

# 容器關閉時一併收掉 Xvfb
trap 'if [ -n "${XVFB_PID:-}" ]; then kill "$XVFB_PID" 2>/dev/null || true; fi' TERM INT EXIT

exec celery -A tasks.celery_app worker --loglevel=info --concurrency=2
