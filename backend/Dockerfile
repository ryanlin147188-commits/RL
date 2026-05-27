FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 系統依賴：mysqlclient 編譯所需（aiomysql/pymysql 為 pure-python 但保留 build-essential 以防日後擴充）
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Backend installs the shared base only. Robotframework-* deps live in
# requirements-celery.txt and are NOT installed here — backend never runs
# tests directly (Celery worker does).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

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
