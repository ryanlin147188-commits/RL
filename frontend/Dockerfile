# AutoTest 前端 image
#
# 用 nginx serve 單檔 SPA(index.html + Tailwind / FontAwesome / Chart.js CDN)。
# build context 必須是 repo root,因為 nginx.conf 跟 index.html 都在 frontend/。

# === Stage 1:從 playwright-core 抽出 trace viewer 靜態檔 ===
# 自托管 Playwright Trace Viewer:外部 trace.playwright.dev 在 HTTPS 環境下
# 沒辦法 fetch 我們 http://192.168.x.x/results/...zip(mixed content 被擋)。
# 跟前端 nginx 同 origin 後就沒這個限制。
FROM node:20-alpine AS trace-viewer-stage
WORKDIR /tmp/pw
RUN npm install --no-save playwright-core@1.49.1
RUN cp -r /tmp/pw/node_modules/playwright-core/lib/vite/traceViewer /trace-viewer
RUN ls /trace-viewer/index.html

# === Stage 2:主 nginx image ===
FROM nginx:1.27-alpine

# 反代設定(把 /api/、/pics/、/results/、/ws/ 反代到 backend / SeaweedFS)
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

# SPA 主檔 + incremental modules
COPY frontend/index.html /usr/share/nginx/html/index.html
COPY frontend/tailwind.css /usr/share/nginx/html/tailwind.css
COPY frontend/js /usr/share/nginx/html/js

# 內建 Playwright Trace Viewer(走 /trace-viewer/index.html?trace=<url>)
COPY --from=trace-viewer-stage /trace-viewer /usr/share/nginx/html/trace-viewer

# Playwright trace viewer 的 index.html 有 <link rel="manifest" href="./manifest.webmanifest">
# 但 dist 沒附這個檔 → nginx try_files fall-through 回 index.html 的 HTML → browser
# 當 JSON parse → console 噴 "Manifest: Line: 2 column: 1 Syntax error"。塞個最小
# 合法 JSON 進去消掉這個雜訊。
RUN echo '{"name":"Playwright Trace Viewer","short_name":"Trace","start_url":"./","display":"standalone"}' \
    > /usr/share/nginx/html/trace-viewer/manifest.webmanifest

# 自簽 TLS 憑證:Playwright Trace Viewer 用 SharedArrayBuffer 解 trace.zip,
# 瀏覽器只在 secure context(HTTPS 或 localhost)放行 SAB。LAN 部署沒辦法
# 弄真的 CA-signed cert,改 build-time 產 10 年自簽,使用者第一次連 https
# 接受警告即可,之後就能用 trace viewer + 一切需要 SAB 的功能。
RUN apk add --no-cache openssl \
    && mkdir -p /etc/nginx/certs \
    && openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout /etc/nginx/certs/server.key \
        -out /etc/nginx/certs/server.crt \
        -subj "/CN=autotest-platform" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:0.0.0.0" \
    && chmod 600 /etc/nginx/certs/server.key

EXPOSE 80 443
