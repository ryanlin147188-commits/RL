# AutoTest 前端 image
#
# 用 nginx serve 單檔 SPA(index.html + Tailwind / FontAwesome / Chart.js CDN)。
# build context 必須是 repo root,因為 nginx.conf 跟 index.html 都在 frontend/。

FROM nginx:1.27-alpine

# 反代設定(把 /api/、/pics/、/results/、/ws/ 反代到 backend / SeaweedFS)
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

# SPA 主檔 + incremental modules
COPY frontend/index.html /usr/share/nginx/html/index.html
COPY frontend/js /usr/share/nginx/html/js

EXPOSE 80
