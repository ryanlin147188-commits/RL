"""OpenClaw sidecar supervisor (Phase 3.5 — real CLI invocation).

Endpoints (on :7950, internal docker network):
  GET  /healthz       — liveness;永遠 200
  GET  /v1/status     — readiness:openclaw CLI 找得到嗎?workspace 有 token 嗎?
  POST /v1/provision  — backend 把 user 的 OpenAI credential 推進來
                        (寫 ~/.openclaw/openclaw.json + workspace .env)
  POST /v1/chat       — spawn `openclaw agent --message ...` 子進程,回 stdout

設計取捨:
- 把每個 user 的 credential 寫到自己的 workspace 目錄(/opt/openclaw-data/<ws>/),
  不共用 ~/.openclaw — 因為 supervisor 容器是 multi-tenant(per-process per-user
  spawn)。supervisor 在 spawn 子進程時 set HOME=<workspace> 讓 openclaw CLI 讀
  workspace 內的 .openclaw/openclaw.json。
- subprocess 用 asyncio.create_subprocess_exec + 60s timeout — 對話超時保護。
- stdout 用作回應內容、stderr 紀錄到 supervisor log;非 zero exit 直接回 502。
- 不真的跑 `openclaw gateway daemon` — 假設 `openclaw agent --message` 能 one-shot
  運作(README 範例直接這樣用);實際運作模型若不同,supervisor 回 503 + reason。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from aiohttp import web

LOG = logging.getLogger("openclaw.supervisor")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

LISTEN_PORT = int(os.environ.get("OPENCLAW_PORT", "7950"))
SIDECAR_AUTH = os.environ.get("OPENCLAW_SIDECAR_AUTH_TOKEN", "").strip()
OPENCLAW_DATA_ROOT = Path(os.environ.get("OPENCLAW_DATA_ROOT", "/opt/openclaw-data"))
CHAT_TIMEOUT_SEC = float(os.environ.get("OPENCLAW_CHAT_TIMEOUT_SEC", "60"))


def _openclaw_bin() -> Optional[str]:
    """找 openclaw CLI 路徑;npm install 失敗時 image build 不會擋,所以這裡判存在。"""
    return shutil.which("openclaw")


def _require_auth(req: web.Request) -> None:
    if not SIDECAR_AUTH:
        return  # dev mode
    got = req.headers.get("X-Sidecar-Auth", "")
    if got != SIDECAR_AUTH:
        raise web.HTTPUnauthorized(reason="bad_sidecar_auth")


async def healthz(_req: web.Request) -> web.Response:
    """Liveness — 永遠 200。"""
    return web.json_response({"status": "ok"})


async def status(req: web.Request) -> web.Response:
    """Readiness — CLI 裝起來了沒?哪些 workspace 已 provision?"""
    _require_auth(req)
    bin_path = _openclaw_bin()
    workspaces = []
    if OPENCLAW_DATA_ROOT.is_dir():
        for ws_dir in OPENCLAW_DATA_ROOT.iterdir():
            if ws_dir.is_dir() and (ws_dir / ".openclaw" / "openclaw.json").is_file():
                workspaces.append(ws_dir.name)

    reasons = []
    if not bin_path:
        reasons.append("openclaw CLI not installed (npm install -g openclaw failed during image build)")
    if not OPENCLAW_DATA_ROOT.is_dir():
        reasons.append(f"data root {OPENCLAW_DATA_ROOT} missing")

    return web.json_response({
        "ready": bool(bin_path) and not reasons,
        "phase": "3.5-cli-shim",
        "openclaw_bin": bin_path,
        "provisioned_workspaces": workspaces,
        "reasons": reasons,
    })


def _build_openclaw_json(oauth_token: str) -> dict:
    """組 ~/.openclaw/openclaw.json 內容。

    讀 docs/llms.txt 推測的結構:
      {
        "agents": { "defaults": { "skipBootstrap": true } },
        "providers": { "openai": { "apiKey": "..." } }
      }
    若實際 schema 不同,supervisor 在 /v1/chat 拿到 openclaw 子進程 stderr 後會
    把錯誤往回傳;backend graceful fallback 不會 break。
    """
    return {
        "agents": {"defaults": {"skipBootstrap": True}},
        "providers": {"openai": {"apiKey": oauth_token}},
    }


async def provision(req: web.Request) -> web.Response:
    """POST /v1/provision — 寫 workspace 內的 .openclaw/openclaw.json + .env。"""
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

    # openclaw CLI 預期 ~/.openclaw/openclaw.json — 我們透過 HOME=$workspace
    # 在 spawn 時 redirect ~/ 過來。
    cfg_dir = home / ".openclaw"
    cfg_dir.mkdir(exist_ok=True)
    cfg_dir.chmod(0o700)
    cfg_path = cfg_dir / "openclaw.json"
    cfg_path.write_text(json.dumps(_build_openclaw_json(token), indent=2))
    cfg_path.chmod(0o600)

    # .env 留一份 plain text 給日後維運 / 未來換 daemon 模式時讀
    env_path = home / ".env"
    env_path.write_text(f"OPENAI_API_KEY={token}\nOPENCLAW_HOME={home}\n")
    env_path.chmod(0o600)

    LOG.info("provisioned openclaw workspace=%s", ws)
    return web.json_response({"workspace_id": ws, "status": "provisioned"})


async def chat(req: web.Request) -> web.Response:
    """POST /v1/chat — spawn `openclaw agent --message ...`、parse stdout。

    成功 (returncode 0)  → 200  {"content": "<stdout>"}
    openclaw 沒裝          → 503  {"error": "openclaw_not_installed", ...}
    workspace 沒 provision → 400  {"error": "workspace_not_provisioned", ...}
    timeout                → 504  {"error": "openclaw_timeout"}
    non-zero exit         → 502  {"error": "openclaw_failed", "stderr": "..."}
    """
    _require_auth(req)
    bin_path = _openclaw_bin()
    if not bin_path:
        return web.json_response(
            {
                "error": "openclaw_not_installed",
                "message": "openclaw CLI 未安裝 — image build 階段 npm install -g openclaw 失敗",
            },
            status=503,
        )

    try:
        body = await req.json()
    except Exception:
        raise web.HTTPBadRequest(reason="invalid_json")
    ws = (body.get("workspace_id") or "").strip()
    prompt = (body.get("prompt") or "").strip()
    if not ws or not prompt:
        raise web.HTTPBadRequest(reason="workspace_id_and_prompt_required")

    home = OPENCLAW_DATA_ROOT / ws
    cfg_path = home / ".openclaw" / "openclaw.json"
    if not cfg_path.is_file():
        return web.json_response(
            {
                "error": "workspace_not_provisioned",
                "message": f"workspace {ws} 尚未 provision — 請先 POST /v1/provision",
            },
            status=400,
        )

    env = dict(os.environ)
    env["HOME"] = str(home)  # 讓 openclaw CLI 讀 workspace 內的 ~/.openclaw
    env["OPENCLAW_HOME"] = str(home)

    LOG.info("openclaw chat ws=%s prompt_len=%d", ws, len(prompt))
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_path, "agent", "--message", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(home),
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=CHAT_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return web.json_response(
                {"error": "openclaw_timeout", "message": f"openclaw agent did not respond within {CHAT_TIMEOUT_SEC}s"},
                status=504,
            )
    except FileNotFoundError:
        return web.json_response(
            {"error": "openclaw_not_installed", "message": "openclaw CLI binary disappeared at runtime"},
            status=503,
        )
    except Exception as e:  # noqa: BLE001
        LOG.exception("openclaw spawn failed ws=%s", ws)
        return web.json_response(
            {"error": "openclaw_spawn_failed", "message": str(e)},
            status=500,
        )

    stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
    stderr = (stderr_b or b"").decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        LOG.warning("openclaw agent rc=%s stderr=%s", proc.returncode, stderr[:500])
        return web.json_response(
            {
                "error": "openclaw_failed",
                "returncode": proc.returncode,
                "stderr": stderr[:2000],
            },
            status=502,
        )

    return web.json_response({"content": stdout, "stderr": stderr[:500] if stderr else None})


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
