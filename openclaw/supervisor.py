"""OpenClaw sidecar supervisor (Phase 3 scaffold).

跑在 :7950(僅 internal docker network),用 aiohttp 提供:
  - GET  /healthz          — 健康檢查(永遠 200,看 daemon ready 旗標另外回)
  - GET  /v1/status        — 回 ready 旗標 + 缺少的東西(OAuth credential / model)
  - POST /v1/chat          — Phase 3 scaffold:501 Not Implemented(待 Phase 3.5 wire
                              到實際 openclaw 子進程)

設計取捨:
- 不在這層 spawn `openclaw onboard --install-daemon` — OpenClaw 需要 ChatGPT OAuth
  device flow,跨 container 操作 OAuth 沒意義(token 也存不下來)。OAuth flow 應該
  在 backend 收回 callback,把 credential 推進來,我們才 spawn daemon。
- /v1/status 讀 RL_OPENCLAW_OAUTH_TOKEN env(supervisor restart 後由 backend 推進來)
  判 ready。沒 token = ready False + reason。
- X-Sidecar-Auth 對齊 hermes/mem0 兩個 sidecar 的鑒權 pattern。

Phase 3.5 要做的:
- /v1/chat:把 prompt 餵給 openclaw daemon(stdio 子進程或 HTTP gateway:18789),
  回收 response stream
- /v1/sessions:create/list session
- OAuth callback handler 在 backend 端,push token 到此 sidecar
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from aiohttp import web

LOG = logging.getLogger("openclaw.supervisor")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

LISTEN_PORT = int(os.environ.get("OPENCLAW_PORT", "7950"))
SIDECAR_AUTH = os.environ.get("OPENCLAW_SIDECAR_AUTH_TOKEN", "").strip()
OPENCLAW_DATA_ROOT = Path(os.environ.get("OPENCLAW_DATA_ROOT", "/opt/openclaw-data"))


def _require_auth(req: web.Request) -> None:
    if not SIDECAR_AUTH:
        return  # dev mode:沒設 secret 就不驗(對齊 hermes/mem0 早期 PR pattern)
    got = req.headers.get("X-Sidecar-Auth", "")
    if got != SIDECAR_AUTH:
        raise web.HTTPUnauthorized(reason="bad_sidecar_auth")


async def healthz(_req: web.Request) -> web.Response:
    """Liveness — 永遠 200。Readiness 看 /v1/status。"""
    return web.json_response({"status": "ok"})


async def status(req: web.Request) -> web.Response:
    """Readiness — 依 OAuth token / data root 判 ready。"""
    _require_auth(req)
    oauth_token = (os.environ.get("RL_OPENCLAW_OAUTH_TOKEN") or "").strip()
    has_data_root = OPENCLAW_DATA_ROOT.is_dir()
    reasons = []
    if not oauth_token:
        reasons.append("missing ChatGPT OAuth credential — backend should push via /v1/provision")
    if not has_data_root:
        reasons.append(f"data root {OPENCLAW_DATA_ROOT} not mounted")
    ready = not reasons
    return web.json_response({
        "ready": ready,
        "phase": "3-scaffold",  # /v1/chat 還沒 wire — 之後 phase 3.5 改 'live'
        "reasons": reasons,
    })


async def provision(req: web.Request) -> web.Response:
    """Phase 3 scaffold:接收 backend 推來的 OAuth credential。

    Backend 完成 ChatGPT device-flow 後 POST 過來,寫進 workspace .env(由
    openclaw daemon 之後讀)。Phase 3.5 才真的 spawn daemon;此版只接收 + 持
    久化讓 /v1/status 反映 ready。
    """
    _require_auth(req)
    try:
        body = await req.json()
    except Exception:
        raise web.HTTPBadRequest(reason="invalid_json")
    ws = (body.get("workspace_id") or "").strip()
    token = (body.get("oauth_token") or "").strip()
    if not ws or not token:
        raise web.HTTPBadRequest(reason="workspace_id_and_oauth_token_required")
    home = OPENCLAW_DATA_ROOT / ws
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    env_path = home / ".env"
    env_path.write_text(f"OPENAI_OAUTH_TOKEN={token}\nOPENCLAW_HOME={home}\n")
    env_path.chmod(0o600)
    LOG.info("provisioned openclaw workspace=%s", ws)
    return web.json_response({"workspace_id": ws, "status": "provisioned"})


async def chat(req: web.Request) -> web.Response:
    """Phase 3 scaffold:回 501 + 解釋。Phase 3.5 才 wire 到真正 daemon。"""
    _require_auth(req)
    return web.json_response(
        {
            "error": "not_implemented",
            "message": "OpenClaw chat is Phase 3.5 — 目前只完成 sidecar scaffold + provision flow,實際對話路徑尚未接通。請暫用 Hermes runtime。",
            "phase": "3-scaffold",
        },
        status=501,
    )


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/v1/status", status)
    app.router.add_post("/v1/provision", provision)
    app.router.add_post("/v1/chat", chat)
    return app


def main() -> None:
    OPENCLAW_DATA_ROOT.mkdir(parents=True, exist_ok=True)
    web.run_app(build_app(), host="0.0.0.0", port=LISTEN_PORT, print=lambda _: None)


if __name__ == "__main__":
    main()
