FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 系統依賴:
# * curl — 基本工具
# * nodejs + npm — Phase 3 MCP stdio:跑 `npx -y @playwright/mcp@latest` 之類
#   官方 MCP server。Debian bookworm 內建 nodejs 18 LTS,大多 MCP server 都吃。
#   若需更新版可換 NodeSource setup,但這層增加 image 約 150MB。
# * ca-certificates — npx 拉套件走 HTTPS 必備
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates nodejs npm && rm -rf /var/lib/apt/lists/*

# Backend installs the shared base only. Robotframework-* deps live in
# requirements-celery.txt and are NOT installed here — backend never runs
# tests directly (Celery worker does).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Phase 3 MCP stdio:`uvx` 用來跑 Python-based MCP server(例:uvx mcp-server-fetch)
# 從 pip 裝 ~10MB,提供 uv / uvx CLI
RUN pip install --no-cache-dir uv==0.5.30

COPY . /app

# PIC 資料夾掛 volume
RUN mkdir -p /app/PIC

# v1.1.14 P1-3:容器降權,不再以 root 跑 uvicorn。
# 若 RCE 進到 backend 也只能拿到 uid=10001 的 shell,降低後續橫向移動空間;
# docker-socket-proxy 已限制 docker.sock,即使是 root 也已無法直接呼叫
# 高權限 docker API,但縱深防禦補上一層仍有意義。
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 啟動 FastAPI（生產用 uvicorn，不開 reload）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
