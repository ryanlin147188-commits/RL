# Frontend 只負責 nginx 反代與靜態資產。
# 進入頁是專案根目錄的 index.html（由 docker-compose 以 volume mount
# 提供），因此不需要 Vite/React build step。
# 如未來想恢復 React SPA，可回溯 git 或重新加入 multi-stage builder。

FROM nginx:1.27-alpine

RUN rm -rf /usr/share/nginx/html/*
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
