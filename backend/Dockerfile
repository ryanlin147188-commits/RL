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

EXPOSE 8000

# 啟動 FastAPI（生產用 uvicorn，不開 reload）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
