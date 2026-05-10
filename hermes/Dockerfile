# Hermes Agent sidecar
# - Per-user workspace 跑在 /opt/data/<workspace_id>/
# - Supervisor 暴露 :7800 給 backend(僅內網)
# - PR1 階段:supervisor 只回 /healthz;hermes-agent 已預裝供後續 PR 使用
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    HERMES_DATA_ROOT=/opt/data

# 系統依賴:hermes-agent 執行期需要 git/ripgrep/ffmpeg;tini 處理 PID 1 訊號
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git ripgrep ffmpeg curl tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Supervisor 自身依賴(hermes-agent 之外的最小集)
RUN pip install --no-cache-dir \
        'aiohttp>=3.9,<4' \
        'pydantic>=2,<3'

# 安裝 hermes-agent + 必要 extras。Hermes 不發 PyPI,只發 git release,
# 使用日期 tag(v2026.5.7 對應內部 0.13.0)。Pinned tag — 升級需重 build + e2e 驗證。
# `mcp` extra 提供 ACP adapter 用的 mcp client SDK;`acp` extra 提供 agent-client-protocol。
RUN pip install --no-cache-dir \
        'hermes-agent[messaging,cron,mcp,honcho,acp] @ git+https://github.com/NousResearch/hermes-agent.git@v2026.5.7'

# 非 root user(uid 10000),per-user workspace 目錄事先建好讓 docker volume 掛入時繼承擁有權
RUN useradd -m -u 10000 -s /bin/bash hermes \
 && mkdir -p /opt/data /opt/hermes \
 && chown -R hermes:hermes /opt/data /opt/hermes \
 && chmod 0700 /opt/data

WORKDIR /opt/hermes

COPY --chown=hermes:hermes supervisor.py entrypoint.sh acp_lockdown.py /opt/hermes/
RUN chmod +x /opt/hermes/entrypoint.sh

USER hermes

EXPOSE 7800

HEALTHCHECK --interval=15s --timeout=5s --retries=6 --start-period=40s \
  CMD curl -fsS http://127.0.0.1:7800/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/hermes/entrypoint.sh"]
