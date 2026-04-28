#!/bin/bash
# Recorder-API 容器 entrypoint:啟 mitmweb(web UI 8081 + HTTP/S proxy 8080)
# + har_dump addon 邊跑邊寫 /work/flows.har;SIGTERM 時把 HAR 上傳到 backend。
#
# 必要環境變數:
#   SESSION_ID   UUID(對應 backend RecordingSession.id)
#   UPLOAD_URL   backend HAR 上傳端點(.../upload-har)
# 可選:
#   PROXY_PORT   預設 8080(對應容器內 mitmproxy listen port)
#   WEB_PORT     預設 8081(mitmweb web UI)
set -u

: "${SESSION_ID:?SESSION_ID is required}"
: "${UPLOAD_URL:?UPLOAD_URL is required}"
PROXY_PORT="${PROXY_PORT:-8080}"
WEB_PORT="${WEB_PORT:-8081}"

mkdir -p /work
export HAR_OUTPUT=/work/flows.har

upload_har() {
    if [ -f "$HAR_OUTPUT" ]; then
        echo "[recorder-api] uploading HAR to $UPLOAD_URL"
        curl -sS --max-time 60 -F "har=@$HAR_OUTPUT" "$UPLOAD_URL" \
            || echo "[recorder-api] upload failed"
    else
        echo "[recorder-api] no HAR yet (no traffic captured)"
    fi
}

cleanup() {
    echo "[recorder-api] SIGTERM received; flushing + uploading"
    upload_har
    exit 0
}
trap cleanup TERM INT

echo "[recorder-api] starting mitmweb"
echo "  proxy:  0.0.0.0:$PROXY_PORT  (user 把瀏覽器 / Postman / app proxy 設這個)"
echo "  web UI: 0.0.0.0:$WEB_PORT    (前端 iframe 連這裡看 captured flows)"
echo "  HAR:    $HAR_OUTPUT"

# --no-web-open-browser:不要嘗試開瀏覽器(容器內無 X)
# --set web_password:不設 → 不需 auth(內部使用)
# --web-host 0.0.0.0:讓 host 能透過 publish port 連
# -s /har_dump.py:載入 HAR addon
mitmweb \
    --listen-host 0.0.0.0 --listen-port "$PROXY_PORT" \
    --web-host 0.0.0.0 --web-port "$WEB_PORT" \
    --no-web-open-browser \
    --set termlog_verbosity=info \
    --set ssl_insecure=true \
    -s /har_dump.py &
MITM_PID=$!

# wait 而不直接 exec mitmweb,以便接 trap 觸發 upload
wait "$MITM_PID"
echo "[recorder-api] mitmweb exited; final upload"
upload_har
