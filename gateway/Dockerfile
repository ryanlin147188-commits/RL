# AutoTest API Gateway — Python FastAPI 流量入口
#
# 跑在 nginx 之後、backend 之前,負責 JWT 驗證、限速、熔斷、API key、觀測。
# 跟 backend 共享 AUTOTEST_JWT_SECRET 做雙層 JWT 驗;另外用
# GATEWAY_BACKEND_SHARED_SECRET HMAC 簽 ``X-Gateway-Verified`` header,
# 讓 backend 端可信任「這個 request 已經被 gateway 驗過」並短路重複 decode。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl 給 healthcheck 用
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/
COPY routes.yaml .

# v1.1.14 P1-3:Gateway 容器降權。Gateway 是流量入口,降權可降低 RCE 後逃逸風險。
# uid 10002 與 backend(10001)區分開,容器間共享 volume 時 uid 不衝突。
RUN useradd --create-home --uid 10002 --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 9000

# 單 worker(請求多再 docker compose scale=N);uvloop + httptools 走 uvicorn standard
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000", "--proxy-headers", "--forwarded-allow-ips", "*"]
