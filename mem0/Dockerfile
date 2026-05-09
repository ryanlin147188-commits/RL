# mem0 sidecar — semantic memory layer
# - Per-user fact storage 走 mem0-postgres(獨立 container,pgvector)
# - Proxy 暴露 :7900 給 backend(僅內網)
# - PR1 階段:proxy 只回 /healthz + ping pg;mem0ai library 留 PR2 才裝
#   (避免本 PR 拖一整套 LLM SDK + numpy/scipy 依賴鏈)
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 系統依賴:tini 處理 PID 1 訊號;curl 給 healthcheck 用
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        curl tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Proxy 自身依賴 + mem0ai library + MCP server lib
# Pin 死版本 — 升級需重 build + e2e 驗證(plan §7 risk #1: schema 升級不可逆)
# `mcp>=1.2,<2` 對齊 hermes container 內 mcp 1.27.x;FastMCP API 在 1.2 後穩定
RUN pip install --no-cache-dir \
        'fastapi>=0.111,<1' \
        'uvicorn[standard]>=0.27,<1' \
        'pydantic>=2,<3' \
        'psycopg[binary,pool]>=3.1,<4' \
        'pgvector>=0.3,<1' \
        'httpx>=0.27,<1' \
        'mem0ai>=0.1.0,<1' \
        'mcp>=1.2,<2'

# 非 root user(uid 10001;與 hermes 的 10000 錯開避免 host 上 file ownership 衝突)
RUN useradd -m -u 10001 -s /bin/bash mem0 \
 && mkdir -p /opt/mem0 \
 && chown -R mem0:mem0 /opt/mem0

WORKDIR /opt/mem0

COPY --chown=mem0:mem0 mem0_proxy.py entrypoint.sh /opt/mem0/
RUN chmod +x /opt/mem0/entrypoint.sh

USER mem0

EXPOSE 7900

HEALTHCHECK --interval=15s --timeout=5s --retries=6 --start-period=30s \
  CMD curl -fsS http://127.0.0.1:7900/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/mem0/entrypoint.sh"]
