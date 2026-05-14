# OpenClaw sidecar (Phase 3.5 — real CLI invocation).
# Base: python:3.11-slim 跑 supervisor.py;之上加 Node 24 跑 openclaw CLI。
# supervisor 在 /v1/chat 路徑 spawn `openclaw agent --message ...` 子進程,parse stdout。
#
# 已知不確定點(留 fallback):
# - openclaw CLI 確切 invocation 介面在 README 看是 `openclaw agent --message X`,
#   但實際操作可能要 `openclaw setup` 先建 ~/.openclaw/openclaw.json。supervisor
#   在 /v1/provision 把這份 config 寫好(skipBootstrap:true + OAuth token)。
# - openclaw 內部對話可能需要 gateway daemon。一律 try `--message` one-shot,失敗
#   就回 503;backend 端會 graceful fallback 回 Hermes。
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OPENCLAW_DATA_ROOT=/opt/openclaw-data \
    NODE_VERSION=24

# Node 24 (NodeSource) + tini + curl
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl tini ca-certificates gnupg \
 && curl -fsSL "https://deb.nodesource.com/setup_${NODE_VERSION}.x" | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && node --version && npm --version

RUN pip install --no-cache-dir 'aiohttp>=3.9,<4'

# 安裝 openclaw 全域 — pin 到 ^0.5 等實 release(release 在 2026Q1 之後 — 若 npm
# 拉不到指定版本,fallback 'openclaw@latest' 然後在 supervisor 紀錄實際裝到的版本)。
# 失敗不擋 image build:讓 image 還是能起 supervisor + healthz,/v1/chat 會回 503
# "openclaw_not_installed";Phase 4 看實況可能要切到自架 git。
RUN npm install -g openclaw@latest 2>&1 \
 || echo "[openclaw] WARN: npm install failed — supervisor 仍會起 /healthz,/v1/chat 回 503"

# 非 root user(uid 10002,與 hermes 10000 / mem0 10001 區隔)
RUN useradd -m -u 10002 -s /bin/bash openclaw \
 && mkdir -p /opt/openclaw-data /opt/openclaw /home/openclaw/.openclaw \
 && chown -R openclaw:openclaw /opt/openclaw-data /opt/openclaw /home/openclaw \
 && chmod 0700 /opt/openclaw-data

WORKDIR /opt/openclaw

COPY --chown=openclaw:openclaw supervisor.py entrypoint.sh /opt/openclaw/
RUN chmod +x /opt/openclaw/entrypoint.sh

USER openclaw

EXPOSE 7950

HEALTHCHECK --interval=15s --timeout=5s --retries=6 --start-period=20s \
  CMD curl -fsS http://127.0.0.1:7950/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/openclaw/entrypoint.sh"]
