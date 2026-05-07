#!/bin/bash
# Recorder 容器 entrypoint:啟 Xvfb + fluxbox + x11vnc + noVNC,然後跑 Playwright codegen
#
# 必要環境變數:
#   TARGET_URL    要錄製的網址
#   SESSION_ID    UUID(對應 backend RecordingSession.id;檔名前綴用)
#   UPLOAD_URL    backend 端點;codegen 結束後自動 multipart 上傳 .py / .zip
# 可選:
#   VNC_PASSWORD  noVNC 連線密碼;預設 "changeme"。backend 啟容器時應 random 一把
#
# 退出條件:
#   1. 使用者在 noVNC 內把 Playwright Inspector 視窗關掉(codegen 自然 exit 0)
#   2. 容器外被 docker stop / rm -f(由 backend 的「停止錄製」按鈕觸發)
set -u
set -o pipefail

: "${TARGET_URL:?TARGET_URL is required}"
: "${SESSION_ID:?SESSION_ID is required}"
: "${UPLOAD_URL:?UPLOAD_URL is required}"
VNC_PASSWORD="${VNC_PASSWORD:-changeme}"

mkdir -p /work
cd /work
mkdir -p "$HOME/.vnc"
x11vnc -storepasswd "$VNC_PASSWORD" "$HOME/.vnc/passwd" >/dev/null 2>&1

cleanup() {
    echo "[recorder] cleanup signal received"
    # docker stop sends SIGTERM. Playwright codegen on SIGTERM exits without
    # flushing the captured actions to the -o file → 「沒抓到腳本」. SIGINT
    # (== user pressing Ctrl+C in the Inspector terminal) is the documented
    # graceful-shutdown signal: codegen prints the script and exits 0.
    # So we send SIGINT first, give Playwright a few seconds to flush, and
    # only fall back to SIGTERM/SIGKILL if it stays alive.
    if [ -n "${CODEGEN_PID:-}" ]; then
        kill -INT "$CODEGEN_PID" 2>/dev/null || true
        # Wait up to 8s for graceful exit. The outer `c.stop(timeout=15)` in
        # backend gives us 15s before SIGKILL — leave 7s headroom.
        for _ in 1 2 3 4 5 6 7 8; do
            kill -0 "$CODEGEN_PID" 2>/dev/null || break
            sleep 1
        done
        # still alive → escalate
        if kill -0 "$CODEGEN_PID" 2>/dev/null; then
            echo "[recorder] codegen survived SIGINT, escalating to SIGTERM"
            kill -TERM "$CODEGEN_PID" 2>/dev/null || true
        fi
    fi
    [ -n "${NGINX_PID:-}" ]   && kill -TERM "$NGINX_PID"   2>/dev/null || true
    [ -n "${WS_PID:-}" ]      && kill -TERM "$WS_PID"      2>/dev/null || true
}
trap cleanup TERM INT

# 1) Xvfb 虛擬顯示
# 1980x1024:讓 Playwright Inspector + 被測網頁兩個視窗能完整並列,
# noVNC 端再以 resize=scale 把整張畫面縮放鋪滿瀏覽器視窗(避免被截掉)。
Xvfb :99 -screen 0 1980x1024x24 -ac +extension GLX +render -noreset >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!
sleep 1
export DISPLAY=:99

# 2) 視窗管理器(fluxbox)— 沒它的話 codegen 會打開「沒邊框」視窗,使用者沒辦法拖
fluxbox >/tmp/fluxbox.log 2>&1 &
FB_PID=$!
sleep 0.5

# 3) x11vnc — 把 :99 對外當 VNC 伺服器(內部 5900,只給 websockify 用)
x11vnc -display :99 \
       -rfbauth "$HOME/.vnc/passwd" \
       -rfbport 5900 \
       -forever -shared -bg -quiet \
       -o /tmp/x11vnc.log
sleep 1

# 4a) websockify on loopback :6081 — only handles the WebSocket-upgraded
#     VNC traffic. nginx serves all the noVNC static files (vnc_lite.html,
#     core/*, app/*) on the public port :6080 and proxies /websockify to
#     this process. We don't pass --web so websockify won't compete with
#     nginx for static-file serving.
websockify 127.0.0.1:6081 localhost:5900 >/tmp/websockify.log 2>&1 &
WS_PID=$!
sleep 1

# 4b) nginx — public-facing on :6080. config in /etc/nginx/conf.d/default.conf
#     was COPYed in by the Dockerfile (recorder_nginx.conf).
nginx -g 'daemon off;' >/tmp/nginx.log 2>&1 &
NGINX_PID=$!
sleep 1

echo "[recorder] noVNC ready on :6080 (nginx) → :6081 (websockify) → :5900 (x11vnc)  display=:99  session=$SESSION_ID"
echo "[recorder] target_url=$TARGET_URL"

# 5) Playwright codegen
SHORT="${SESSION_ID:0:8}"
PY="recorded_${SHORT}.py"
TZ="trace_${SHORT}.zip"

# `playwright codegen` no longer ships a --save-trace flag (the option
# was removed in 1.50; only --save-har / --save-storage remain). The
# script.py is the primary deliverable, trace.zip becomes optional —
# if the user wants traces they can add a tracing.start() block manually
# to the recorded script before re-running it.
python3 -m playwright codegen \
    --target python \
    -o "$PY" \
    "$TARGET_URL" 2>&1 | tee /tmp/codegen.log &
CODEGEN_PID=$!
wait "$CODEGEN_PID" || true

# 6) 自動上傳(best-effort;失敗也不擋容器退出)
echo "[recorder] codegen exited;uploading to $UPLOAD_URL"
if [ -f "$PY" ]; then
    PY_SIZE=$(wc -c < "$PY")
    echo "[recorder] script file: $PY ($PY_SIZE bytes)"
else
    echo "[recorder] WARNING: $PY not found — codegen may have crashed before writing"
fi
if [ -f "$TZ" ]; then
    TZ_SIZE=$(wc -c < "$TZ")
    echo "[recorder] trace file:  $TZ  ($TZ_SIZE bytes)"
fi
ARGS=()
if [ -f "$PY" ]; then ARGS+=(-F "script=@$PY"); fi
if [ -f "$TZ" ]; then ARGS+=(-F "trace=@$TZ"); fi
if [ "${#ARGS[@]}" -eq 0 ]; then
    echo "[recorder] no output to upload — codegen may have crashed"
else
    # -w 印 HTTP code 跟 response;-o 把 body 收到 /tmp 方便 debug
    HTTP_CODE=$(curl -sS --max-time 60 -o /tmp/upload_resp.txt -w "%{http_code}" \
        "${ARGS[@]}" "$UPLOAD_URL" 2>/tmp/upload_err.txt || echo "000")
    if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ] || [ "$HTTP_CODE" = "204" ]; then
        echo "[recorder] upload OK (HTTP $HTTP_CODE)"
    else
        echo "[recorder] upload FAILED — HTTP $HTTP_CODE"
        echo "[recorder] curl stderr:"; cat /tmp/upload_err.txt 2>/dev/null || true
        echo "[recorder] response body:"; head -c 500 /tmp/upload_resp.txt 2>/dev/null || true; echo ""
    fi
fi

echo "[recorder] done"
# 容器退出 → docker --rm 自動清理
