# mem0 sidecar — FastAPI 包 mem0ai,backend 透過 docker network 連 :7900
# 跑 unprivileged user(uid 10001),沒對外暴露 port(只在 docker network 內)。
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY mem0-sidecar/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY mem0-sidecar/main.py /app/main.py

# 跑 unprivileged user
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin sidecar && \
    chown -R sidecar:sidecar /app
USER sidecar

EXPOSE 7900
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7900", "--workers", "1"]
