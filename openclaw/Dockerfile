# OpenClaw sidecar (Phase 3 scaffold).
# 預備跑 OpenClaw gateway daemon(Node 24);但目前 image 只裝最小 supervisor
# 給 /healthz + /v1/provision;Phase 3.5 wire OAuth + daemon 時再升級 base 加 Node。
#
# 設計取捨:不在 scaffold 階段裝 Node + openclaw,因為:
# 1) 沒 OAuth credential 時 daemon 啟不起來,裝了也是死負重
# 2) 加 Node 24 + npm package 會把 image 從 ~80MB 推到 ~600MB
# 3) supervisor 的 /v1/provision 收到 backend 推來的 OAuth token 後,Phase 3.5
#    再 spawn 子進程跑 daemon(image 升級成 node 24 base + apt install python3)
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OPENCLAW_DATA_ROOT=/opt/openclaw-data

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir 'aiohttp>=3.9,<4'

# 非 root user(uid 10002,與 hermes 10000 / mem0 10001 區隔)
RUN useradd -m -u 10002 -s /bin/bash openclaw \
 && mkdir -p /opt/openclaw-data /opt/openclaw \
 && chown -R openclaw:openclaw /opt/openclaw-data /opt/openclaw \
 && chmod 0700 /opt/openclaw-data

WORKDIR /opt/openclaw

COPY --chown=openclaw:openclaw supervisor.py entrypoint.sh /opt/openclaw/
RUN chmod +x /opt/openclaw/entrypoint.sh

USER openclaw

EXPOSE 7950

HEALTHCHECK --interval=15s --timeout=5s --retries=6 --start-period=20s \
  CMD curl -fsS http://127.0.0.1:7950/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/openclaw/entrypoint.sh"]
