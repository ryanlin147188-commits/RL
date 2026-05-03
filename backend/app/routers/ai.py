"""AI 相關 REST endpoints — 目前包含「從需求生成測試案例」MVP。

(同 auth.py：刻意不開啟 `from __future__ import annotations`，避免
與 slowapi `@limiter.limit` 的型別內省衝突。)
"""
import asyncio
import json
import logging
import os
import re
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

log = logging.getLogger(__name__)
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.requirement import Requirement
from app.models.user import User
from app.rate_limit import limiter
from app.services.ai_test_gen import (
    generate_testcases_from_requirement,
    generate_testcases_from_text,
)

router = APIRouter()


class AiGenerateRequest(BaseModel):
    n: int = 3
    provider: Optional[str] = None  # 不指定 → 用系統預設


class AiGeneratedStep(BaseModel):
    keyword: str = "When"
    description: str = ""
    action: str = ""
    locator: str = ""
    input: str = ""
    condition: str = "Equals"
    expected: str = ""


class AiGeneratedItem(BaseModel):
    title: str
    ac: str
    steps_md: str
    # Sprint 1.3 — 新增:LLM 直接回的 step 陣列(GeneratedStep 結構)
    # 前端可一鍵套用到既有案例或新建案例;若空,fallback 走 steps_md 人工編輯
    steps_json: list[AiGeneratedStep] = []


class AiGenerateResponse(BaseModel):
    provider: str
    model: str
    generated: list[AiGeneratedItem]
    raw: Optional[str] = None
    error: Optional[str] = None


@router.post(
    "/ai/generate-testcases-from-requirement/{req_id}",
    response_model=AiGenerateResponse,
    tags=["V · AI"],
)
@limiter.limit("30/hour")            # AI 是昂貴資源：每使用者每小時 30 次（about 1 次/2 分鐘）
async def generate_testcases_from_req(
    request: Request,
    req_id: str,
    payload: AiGenerateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    req = await db.get(Requirement, req_id)
    if not req:
        raise HTTPException(404, "需求不存在")
    try:
        result = await generate_testcases_from_requirement(
            db, req, n=payload.n, provider=payload.provider,
            organization_id=user.organization_id,
        )
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # 收所有 httpx / json / 其他例外
        raise HTTPException(502, f"AI 服務呼叫失敗：{e}")
    return result


# Sprint 2.3 — 從純文字產測試案例(AI Chat 訊息 / 任意需求描述都可用)
class AiGenerateFromTextRequest(BaseModel):
    text: str
    n: int = 3
    provider: Optional[str] = None


@router.post(
    "/ai/generate-testcases",
    response_model=AiGenerateResponse,
    tags=["V · AI"],
)
@limiter.limit("30/hour")
async def generate_testcases_from_text_ep(
    request: Request,
    payload: AiGenerateFromTextRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not (payload.text or "").strip():
        raise HTTPException(400, "text 不能為空")
    try:
        result = await generate_testcases_from_text(
            db, payload.text, n=payload.n, provider=payload.provider,
            organization_id=user.organization_id,
        )
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"AI 服務呼叫失敗：{e}")
    return result


# ─────────────────────────────────────────────────────────
# Sprint 4.3 — Playwright MCP server PoC
# 一次啟一個 MCP 容器(只允許一個 active session,簡化狀態管理)
# 完整 AI tool-calling 整合(多輪對話 / function calling)留作後續
# ─────────────────────────────────────────────────────────

import secrets
from datetime import datetime, timedelta
from app.config import settings as _settings


class McpStatus(BaseModel):
    running: bool
    container_id: Optional[str] = None
    container_name: Optional[str] = None
    sse_port: Optional[int] = None
    sse_url: Optional[str] = None  # 對前端的相對 URL(host 由前端 window.location 補)
    started_at: Optional[datetime] = None


# Sprint 9.1 — Per-user MCP container 狀態。
# key = username,value = dict(container_id / container_name / sse_port / vnc_password / started_at / expires_at)
# 沿用既有 _mcp_state 變數名給舊程式碼相容(指向當前 user 的子 dict);
# 真正資料存在 _mcp_state_by_user。
_mcp_state_by_user: dict[str, dict] = {}


def _get_user_mcp_state(username: str) -> dict:
    """取或建立當前使用者的 MCP state。"""
    if username not in _mcp_state_by_user:
        _mcp_state_by_user[username] = {}
    return _mcp_state_by_user[username]


# Sprint 9.2 — Run loop abort flag,key = username
_mcp_run_abort_flags: dict[str, bool] = {}

# Sprint 10.2 — 紀錄正在跑的 mcp_run asyncio task,讓 /run-abort 即時 cancel
_mcp_run_tasks: dict[str, "asyncio.Task"] = {}


def _touch_mcp(username: str) -> None:
    """Sprint 10.1 — user 操作 MCP(start / tools / call / run)時更新 last_active_at,
    讓 idle sweeper 不會誤殺正在用的容器。"""
    state = _mcp_state_by_user.get(username)
    if state and state.get("container_id"):
        state["last_active_at"] = datetime.utcnow()


async def _mcp_idle_sweeper_loop():
    """Sprint 10.1 — 每 60 秒掃 _mcp_state_by_user,找超過 IDLE_TIMEOUT_MIN
    沒活動的 entry → docker stop + 清 state。

    沿用 settings.RECORDER_IDLE_TIMEOUT_MIN(預設 30 分鐘)。
    """
    while True:
        try:
            await asyncio.sleep(60)
            timeout_min = int(getattr(_settings, "RECORDER_IDLE_TIMEOUT_MIN", 30) or 30)
            now = datetime.utcnow()
            cutoff = now - timedelta(minutes=timeout_min)
            stale = []
            for username, state in list(_mcp_state_by_user.items()):
                la = state.get("last_active_at") or state.get("started_at")
                if la and la < cutoff and state.get("container_id"):
                    stale.append((username, state))
            if not stale:
                continue
            try:
                client = _get_mcp_docker()
            except Exception as e:
                log.warning("MCP sweeper:無法連 docker daemon: %s", e)
                continue
            for username, state in stale:
                cid = state.get("container_id")
                cname = state.get("container_name")
                try:
                    c = client.containers.get(cid)
                    try:
                        c.stop(timeout=10)
                    except Exception:
                        pass
                    log.info("MCP sweeper:回收 idle container user=%s name=%s",
                             username, cname)
                except Exception as e:
                    log.info("MCP sweeper:容器 %s 已不在(%s),清 state", cname, e)
                _mcp_state_by_user.pop(username, None)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.exception("MCP sweeper 例外: %s", e)
            await asyncio.sleep(60)


# 舊變數名 alias:有些函式吃 module-level 全域(_mcp_jsonrpc / 三條 loop),
# 我改成傳 username 進去 → 從 by_user dict 取。為了向後相容,_mcp_state 不再用,
# 全部都走 _get_user_mcp_state(username)。


def _get_mcp_docker():
    try:
        import docker  # type: ignore
    except ImportError:
        raise HTTPException(500, "後端缺少 docker 套件")
    try:
        return docker.from_env()
    except Exception as e:
        raise HTTPException(500, f"無法連到 docker daemon:{e}")


# ─── Sprint 6.1 — MCP image 自動 build(沿用 recorder 425+polling 模式) ───
_mcp_image_state: dict = {
    "status": "unknown",  # unknown / missing / building / ready / error
    "log": [],
    "started_at": None,
    "finished_at": None,
    "error": None,
}
_mcp_image_lock = asyncio.Lock()


def _mcp_image_exists() -> bool:
    try:
        client = _get_mcp_docker()
    except HTTPException:
        return False
    try:
        client.images.get(_settings.MCP_IMAGE)
        return True
    except Exception:
        return False


def _mcp_log_append(line: str, max_lines: int = 200) -> None:
    state = _mcp_image_state
    state["log"].append(line)
    if len(state["log"]) > max_lines:
        state["log"] = state["log"][-max_lines:]


def _build_mcp_image_sync() -> None:
    """Blocking build for asyncio.to_thread。Build context 用 backend image 內 /app
    (Dockerfile 已 COPY . /app,所以 Dockerfile.mcp 在 /app/Dockerfile.mcp)。"""
    import docker  # type: ignore
    state = _mcp_image_state
    try:
        api = docker.APIClient(
            base_url=os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
        )
    except Exception as e:
        state["status"] = "error"
        state["error"] = f"連不上 docker daemon:{e}"
        state["finished_at"] = datetime.utcnow()
        return

    state["status"] = "building"
    state["log"] = []
    state["error"] = None
    state["started_at"] = datetime.utcnow()
    state["finished_at"] = None
    _mcp_log_append(f"[backend] 開始 build {_settings.MCP_IMAGE}")
    _mcp_log_append("[backend] context=/app  dockerfile=Dockerfile.mcp")
    try:
        for chunk in api.build(
            path="/app",
            dockerfile="Dockerfile.mcp",
            tag=_settings.MCP_IMAGE,
            rm=True, forcerm=True, decode=True, pull=False,
        ):
            if "stream" in chunk:
                for ln in chunk["stream"].splitlines():
                    if ln.strip():
                        _mcp_log_append(ln.rstrip())
            elif "status" in chunk:
                msg = chunk["status"]
                if "id" in chunk:
                    msg = f"{chunk['id']}: {msg}"
                _mcp_log_append(msg)
            elif "errorDetail" in chunk or "error" in chunk:
                err = chunk.get("errorDetail", {}).get("message") or chunk.get("error")
                _mcp_log_append(f"[error] {err}")
                state["status"] = "error"
                state["error"] = err
                state["finished_at"] = datetime.utcnow()
                return
        if _mcp_image_exists():
            state["status"] = "ready"
            _mcp_log_append(f"[backend] build done, image={_settings.MCP_IMAGE}")
        else:
            state["status"] = "error"
            state["error"] = "build 結束但 image 不存在"
        state["finished_at"] = datetime.utcnow()
    except Exception as e:
        log.exception("mcp image build failed")
        _mcp_log_append(f"[exception] {type(e).__name__}: {e}")
        state["status"] = "error"
        state["error"] = f"{type(e).__name__}: {e}"
        state["finished_at"] = datetime.utcnow()


async def _trigger_mcp_build_if_needed() -> str:
    async with _mcp_image_lock:
        state = _mcp_image_state
        if state["status"] == "building":
            return "building"
        if _mcp_image_exists():
            state["status"] = "ready"
            return "ready"
        state["status"] = "building"
        asyncio.create_task(asyncio.to_thread(_build_mcp_image_sync))
        return "building"


class McpImageStatus(BaseModel):
    status: str  # missing / building / ready / error / unknown
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    log_tail: list[str] = []


@router.get("/ai/mcp/image-status", response_model=McpImageStatus, tags=["V · AI"])
async def mcp_image_status():
    state = _mcp_image_state
    if state["status"] in ("unknown", "missing") and _mcp_image_exists():
        state["status"] = "ready"
    return McpImageStatus(
        status=state["status"] if state["status"] != "unknown"
            else ("ready" if _mcp_image_exists() else "missing"),
        error=state["error"],
        started_at=state["started_at"],
        finished_at=state["finished_at"],
        log_tail=list(state["log"][-80:]),
    )


@router.post("/ai/mcp/image-build", response_model=McpImageStatus, tags=["V · AI"])
async def trigger_mcp_image_build():
    new_status = await _trigger_mcp_build_if_needed()
    state = _mcp_image_state
    return McpImageStatus(
        status=new_status, error=state["error"],
        started_at=state["started_at"], finished_at=state["finished_at"],
        log_tail=list(state["log"][-80:]),
    )


@router.post("/ai/mcp/start", response_model=McpStatus, tags=["V · AI"])
async def mcp_start(user: User = Depends(get_current_user)):
    """啟一個 Playwright MCP server 容器(per-user;若該 user 已有跑著的不重啟,直接回現狀)。"""
    state = _get_user_mcp_state(user.username)
    if state.get("container_id"):
        # 驗證仍 alive
        try:
            client = _get_mcp_docker()
            c = client.containers.get(state["container_id"])
            if c.status in ("running", "created"):
                return McpStatus(
                    running=True,
                    container_id=state["container_id"],
                    container_name=state.get("container_name"),
                    sse_port=state.get("sse_port"),
                    sse_url=f"/mcp/sse?port={state.get('sse_port')}",
                    started_at=state.get("started_at"),
                )
        except Exception:
            state.clear()

    client = _get_mcp_docker()
    try:
        client.images.get(_settings.MCP_IMAGE)
    except Exception:
        # Sprint 6.1 — image missing 時觸發背景 build,前端 polling /image-status
        new_status = await _trigger_mcp_build_if_needed()
        if new_status != "ready":
            raise HTTPException(
                status_code=425,
                detail={
                    "code": "mcp_image_building",
                    "message": (
                        f"MCP image 還沒 build 完(image={_settings.MCP_IMAGE});"
                        "後端已自動開始 build,請 polling /api/ai/mcp/image-status 等 ready 後重試"
                    ),
                    "status": new_status,
                },
            )

    # Sprint 9.1 — container name 加 user-safe slug,避免不同 user 撞名
    user_slug = re.sub(r"[^A-Za-z0-9_-]", "_", user.username)[:24]
    name = f"autotest-mcp-{user_slug}-{secrets.token_hex(4)}"
    try:
        c = client.containers.run(
            image=_settings.MCP_IMAGE,
            name=name,
            detach=True,
            auto_remove=True,
            network=_settings.RECORDER_NETWORK,
            ports={"8931/tcp": None},
            labels={
                "autotest.role": "mcp",
                "autotest.user": user.username,
            },
        )
    except Exception as e:
        raise HTTPException(500, f"啟動 MCP 容器失敗:{e}")
    c.reload()
    port_info = (c.attrs.get("NetworkSettings", {}).get("Ports") or {}).get("8931/tcp")
    if not port_info:
        try: c.remove(force=True)
        except Exception: pass
        raise HTTPException(500, "MCP 容器啟動但 8931 未對外映射")
    sse_port = int(port_info[0]["HostPort"])
    started_at = datetime.utcnow()
    state.update({
        "container_id": c.id,
        "container_name": name,
        "sse_port": sse_port,
        "started_at": started_at,
        "last_active_at": started_at,  # Sprint 10.1
    })
    return McpStatus(
        running=True,
        container_id=c.id,
        container_name=name,
        sse_port=sse_port,
        sse_url=f"/mcp/sse?port={sse_port}",
        started_at=started_at,
    )


@router.post("/ai/mcp/stop", status_code=204, tags=["V · AI"])
async def mcp_stop(user: User = Depends(get_current_user)):
    state = _get_user_mcp_state(user.username)
    info = dict(state)
    state.clear()
    if not info.get("container_id"):
        return
    try:
        client = _get_mcp_docker()
        c = client.containers.get(info["container_id"])
        try: c.stop(timeout=10)
        except Exception: pass
    except Exception:
        pass


class McpToolInfo(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: Optional[dict] = None


class McpToolsResponse(BaseModel):
    running: bool
    tool_count: int = 0
    tools: list[McpToolInfo] = []
    error: Optional[str] = None


@router.get("/ai/mcp/tools", response_model=McpToolsResponse, tags=["V · AI"])
async def mcp_tools(user: User = Depends(get_current_user)):
    """Sprint 5.3 — 從正在跑的 MCP server 抓可用 tool 清單。

    Playwright MCP server 在 SSE 模式下接受 JSON-RPC over HTTP POST 到 /mcp endpoint。
    透過 `tools/list` method 拿可用 tool。前端可用此清單顯示給使用者「LLM 能做哪些事」。
    """
    state = _get_user_mcp_state(user.username)
    if not state.get("container_id") or not state.get("container_name"):
        return McpToolsResponse(running=False, error="MCP server 沒在跑;請先 POST /api/ai/mcp/start")
    _touch_mcp(user.username)

    # 從 backend 容器內走 internal docker network 連 MCP server(用 container_name 當 hostname)
    mcp_url = f"http://{state['container_name']}:8931/mcp"
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    }
    headers = {
        "Content-Type": "application/json",
        # MCP SSE protocol 接受 JSON 回應或 SSE event-stream
        "Accept": "application/json, text/event-stream",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(mcp_url, json=payload, headers=headers)
            r.raise_for_status()
            # MCP server 可能回 JSON-RPC response 或 SSE event 包裹的 JSON
            text = r.text
            data: dict
            if text.startswith("event:") or text.startswith("data:"):
                # SSE:抓 data: 後的 JSON line
                for line in text.splitlines():
                    if line.startswith("data:"):
                        try:
                            data = json.loads(line[5:].strip())
                            break
                        except json.JSONDecodeError:
                            continue
                else:
                    return McpToolsResponse(running=True, error="無法解析 SSE 回應")
            else:
                data = r.json()
    except httpx.HTTPError as e:
        return McpToolsResponse(running=True, error=f"MCP server 連線失敗:{e}")
    except Exception as e:
        return McpToolsResponse(running=True, error=f"未預期錯誤:{e}")

    if "error" in data:
        return McpToolsResponse(running=True, error=str(data["error"]))
    tools_raw = (data.get("result") or {}).get("tools") or []
    tools = [
        McpToolInfo(
            name=str(t.get("name") or ""),
            description=t.get("description"),
            input_schema=t.get("inputSchema") or t.get("input_schema"),
        )
        for t in tools_raw if isinstance(t, dict)
    ]
    return McpToolsResponse(running=True, tool_count=len(tools), tools=tools)


class McpCallRequest(BaseModel):
    tool: str
    arguments: dict = {}
    timeout: int = 30


class McpCallResponse(BaseModel):
    ok: bool
    tool: str
    result: Optional[dict] = None
    content: Optional[list] = None  # MCP tool/call 標準回的 content array
    error: Optional[str] = None


@router.post("/ai/mcp/call", response_model=McpCallResponse, tags=["V · AI"])
async def mcp_call(payload: McpCallRequest, user: User = Depends(get_current_user)):
    """Sprint 6.2 — 對 MCP server 發單次 JSON-RPC `tools/call`,回 result。
    用來:
    - 前端「測試 MCP」面板手動驗證 tool 能不能跑
    - 後續完整 LLM tool calling loop 的內部 RPC building block
    """
    state = _get_user_mcp_state(user.username)
    if not state.get("container_name"):
        raise HTTPException(409, "MCP server 沒在跑;請先 POST /api/ai/mcp/start")
    _touch_mcp(user.username)

    mcp_url = f"http://{state['container_name']}:8931/mcp"
    payload_rpc = {
        "jsonrpc": "2.0", "id": 2,
        "method": "tools/call",
        "params": {"name": payload.tool, "arguments": payload.arguments or {}},
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    timeout = max(5, min(payload.timeout or 30, 120))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(mcp_url, json=payload_rpc, headers=headers)
            r.raise_for_status()
            text = r.text
            data: dict
            if text.startswith("event:") or text.startswith("data:"):
                data = {}
                for line in text.splitlines():
                    if line.startswith("data:"):
                        try:
                            data = json.loads(line[5:].strip())
                            break
                        except json.JSONDecodeError:
                            continue
            else:
                data = r.json()
    except httpx.HTTPError as e:
        return McpCallResponse(ok=False, tool=payload.tool, error=f"MCP server 連線失敗:{e}")
    except Exception as e:
        return McpCallResponse(ok=False, tool=payload.tool, error=f"未預期錯誤:{e}")

    if "error" in data:
        return McpCallResponse(ok=False, tool=payload.tool, error=str(data["error"]))
    result = data.get("result") or {}
    return McpCallResponse(
        ok=True, tool=payload.tool,
        result=result if isinstance(result, dict) else {"value": result},
        content=result.get("content") if isinstance(result, dict) else None,
    )


# ─── Sprint 7 — LLM ↔ MCP multi-turn tool calling loop ────────────────
# 流程:
#   1. 確保 MCP container 跑著(沒跑自動 start)
#   2. 從 MCP server 抓 tools/list → 轉成 OpenAI tools schema
#   3. 進 loop:LLM 回 tool_calls → 對每個 call 透過 MCP 跑 → result 加進 messages
#   4. LLM finish_reason=stop 或達 max_turns → 結束
#   5. 從 LLM 最後輸出抽 steps_json(prompt 要求 LLM 完成後回 GeneratedStep 陣列)
#
# 目前只做 OpenAI / OpenAI-compatible(Local/Ollama/DeepSeek 等)。
# Anthropic / Google 的 tools 格式不同,留下個 sprint 補齊;
# 走非 OpenAI provider 會回 501 Not Implemented。

class McpRunRequest(BaseModel):
    prompt: str
    target_url: Optional[str] = None
    max_turns: int = 10
    provider: Optional[str] = None


class McpRunResponse(BaseModel):
    ok: bool
    turns: int
    finish_reason: Optional[str] = None
    final_text: Optional[str] = None
    steps_json: list[dict] = []
    tool_calls_log: list[dict] = []
    error: Optional[str] = None


_MCP_LOOP_SYSTEM_PROMPT = """你是 QA 自動化工程師,你能透過 Playwright MCP tools 操作真實瀏覽器來探索受測網站,然後產出一組可重現的測試案例 step。

工作流:
1. 收到 user 的測試需求 + 起始 URL,先用 navigate / browser_navigate 開頁
2. 用 browser_snapshot / browser_click / browser_type 等 tool 模擬使用者操作
3. **完成探索後**,用最後一個 assistant message 回傳 JSON 陣列(GeneratedStep 結構),
   把剛才的操作翻譯成可重現的測試 step。**不要有圍欄、解釋。**

GeneratedStep 結構:
{"keyword": "Given|When|Then|And", "description": "...", "action": "Goto|Click|Fill|Press|AssertText|AssertVisible|...", "locator": "CSS / role= / text=", "input": "...", "condition": "Equals|Contains|...", "expected": "..."}

注意:
- tool 互動目的是「實際看到頁面回應」推斷正確 step
- 最終輸出要產 5-15 個 step;包含至少一個 AssertText / AssertVisible 斷言
- 若需求過於抽象 → tool 探索 1-2 步即可,直接合理推斷產 step
"""


def _mcp_tools_to_openai_schema(mcp_tools: list[dict]) -> list[dict]:
    """把 MCP 的 tool definition 轉成 OpenAI tools format。"""
    out = []
    for t in mcp_tools or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        schema = t.get("input_schema") or t.get("inputSchema") or {"type": "object", "properties": {}}
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": (t.get("description") or "")[:1024],
                "parameters": schema,
            },
        })
    return out


async def _mcp_jsonrpc(method: str, params: dict, *, username: str, timeout: int = 30) -> dict:
    """純 JSON-RPC 呼叫 MCP server(共用工具,Sprint 9.1 改成 per-user)。"""
    state = _get_user_mcp_state(username)
    if not state.get("container_name"):
        raise RuntimeError("MCP server 沒在跑")
    mcp_url = f"http://{state['container_name']}:8931/mcp"
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(mcp_url, json=payload, headers=headers)
        r.raise_for_status()
        text = r.text
        if text.startswith("event:") or text.startswith("data:"):
            for line in text.splitlines():
                if line.startswith("data:"):
                    try:
                        return json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
            raise RuntimeError("無法解析 SSE 回應")
        return r.json()


def _extract_steps_from_text(text: str) -> list[dict]:
    """從 LLM 最後輸出抽 step 陣列(同 ai_test_gen 的解析邏輯)。"""
    if not text:
        return []
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(),
                     flags=re.IGNORECASE | re.MULTILINE)
    m = re.search(r"\[[\s\S]*\]", cleaned)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for s in data:
        if not isinstance(s, dict):
            continue
        out.append({
            "keyword": str(s.get("keyword") or "When").strip()[:10],
            "description": str(s.get("description") or "").strip()[:300],
            "action": str(s.get("action") or "").strip()[:40],
            "locator": str(s.get("locator") or "").strip()[:500],
            "input": str(s.get("input") or "").strip()[:2000],
            "condition": str(s.get("condition") or "Equals").strip()[:40],
            "expected": str(s.get("expected") or "").strip()[:500],
        })
    return out


async def _run_mcp_loop(
    token,
    prompt: str,
    target_url: Optional[str],
    max_turns: int,
    mcp_tools: list[dict],
    username: str,
) -> dict:
    """OpenAI / OpenAI-compatible 的多輪 tool-calling loop(Sprint 9.1 加 username,Sprint 9.2 加 abort)。"""
    base_url = token.base_url or {
        "OpenAI": "https://api.openai.com/v1",
        "Local": "http://host.docker.internal:11434/v1",
    }.get(str(token.provider).split(".")[-1] if hasattr(token.provider, "value") else token.provider, "")
    if not base_url:
        from app.services.ai_test_gen import _default_base_url, _default_model
        base_url = _default_base_url(token.provider)
    model = token.model or "gpt-4o-mini"

    headers = {"Content-Type": "application/json"}
    if token.api_key:
        headers["Authorization"] = f"Bearer {token.api_key}"

    user_intro = f"# 測試需求\n{prompt}"
    if target_url:
        user_intro += f"\n\n# 起始 URL\n{target_url}\n\n請用 tools 開頁並探索。"

    messages: list[dict] = [
        {"role": "system", "content": _MCP_LOOP_SYSTEM_PROMPT},
        {"role": "user", "content": user_intro},
    ]
    tools_schema = _mcp_tools_to_openai_schema(mcp_tools)
    tool_calls_log: list[dict] = []
    final_text: Optional[str] = None
    finish_reason: Optional[str] = None
    last_turn = 0

    async with httpx.AsyncClient(timeout=120.0) as client:
        for turn in range(max_turns):
            last_turn = turn + 1
            # Sprint 9.2 — abort 檢查
            if _mcp_run_abort_flags.get(username):
                _mcp_run_abort_flags.pop(username, None)
                final_text = "(使用者取消)"
                finish_reason = "aborted"
                break
            payload: dict = {
                "model": model,
                "messages": messages,
                "temperature": 0.2,
            }
            if tools_schema:
                payload["tools"] = tools_schema
                payload["tool_choice"] = "auto"
            url = base_url.rstrip("/") + "/chat/completions"
            try:
                r = await client.post(url, headers=headers, json=payload)
                r.raise_for_status()
                resp = r.json()
            except Exception as e:
                raise RuntimeError(f"LLM 呼叫失敗(turn {turn+1}):{e}")

            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            finish_reason = choice.get("finish_reason")
            content = msg.get("content")
            tool_calls = msg.get("tool_calls") or []

            if not tool_calls:
                # LLM 不再呼叫 tool → 結束 loop
                final_text = content or ""
                break

            # 把 assistant 的 tool_calls 訊息加進 history
            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            })

            # 對每個 tool_call 透過 MCP 執行
            for tc in tool_calls:
                fn = tc.get("function") or {}
                tool_name = fn.get("name") or ""
                args_raw = fn.get("arguments") or "{}"
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except Exception:
                    args = {}
                tool_result_text: str
                try:
                    rpc = await _mcp_jsonrpc("tools/call", {"name": tool_name, "arguments": args}, username=username, timeout=45)
                    if "error" in rpc:
                        tool_result_text = json.dumps({"error": str(rpc["error"])}, ensure_ascii=False)
                    else:
                        rr = rpc.get("result") or {}
                        # MCP tool result 通常含 content 陣列;只取 text 部分(避免巨大 base64 圖塞 LLM)
                        if isinstance(rr, dict):
                            text_parts = []
                            for c in (rr.get("content") or []):
                                if isinstance(c, dict) and c.get("type") == "text":
                                    text_parts.append(c.get("text") or "")
                            tool_result_text = "\n".join(text_parts) if text_parts else json.dumps(rr, ensure_ascii=False)[:4000]
                        else:
                            tool_result_text = str(rr)[:4000]
                except Exception as e:
                    tool_result_text = json.dumps({"error": f"MCP call exception: {e}"}, ensure_ascii=False)

                tool_calls_log.append({
                    "turn": turn + 1, "tool": tool_name,
                    "arguments": args, "result_preview": tool_result_text[:300],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id") or f"call_{len(tool_calls_log)}",
                    "name": tool_name,
                    "content": tool_result_text,
                })
        else:
            # 跑滿 max_turns 仍沒 finish → 強制要 LLM 給最終 step list
            messages.append({
                "role": "user",
                "content": "已經操作很多輪了,請立刻停止 tool 呼叫,直接輸出 GeneratedStep JSON 陣列(沒圍欄沒解釋)。",
            })
            try:
                r = await client.post(
                    base_url.rstrip("/") + "/chat/completions",
                    headers=headers,
                    json={"model": model, "messages": messages, "temperature": 0.2},
                )
                r.raise_for_status()
                resp = r.json()
                final_text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                finish_reason = "stop_forced"
            except Exception as e:
                final_text = ""
                finish_reason = f"error_after_max_turns:{e}"

    steps = _extract_steps_from_text(final_text or "")
    return {
        "ok": bool(steps),
        "turns": last_turn,
        "finish_reason": finish_reason,
        "final_text": (final_text or "")[:4000],
        "steps_json": steps,
        "tool_calls_log": tool_calls_log,
    }


# ─── Sprint 8.a — Anthropic Claude tools loop ─────────────────────────
def _mcp_tools_to_anthropic_schema(mcp_tools: list[dict]) -> list[dict]:
    """Anthropic Messages API tools 格式:{name, description, input_schema}。"""
    out = []
    for t in mcp_tools or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        schema = t.get("input_schema") or t.get("inputSchema") or {"type": "object", "properties": {}}
        out.append({
            "name": t["name"],
            "description": (t.get("description") or "")[:1024],
            "input_schema": schema,
        })
    return out


async def _run_mcp_loop_anthropic(
    token, prompt: str, target_url: Optional[str], max_turns: int, mcp_tools: list[dict], username: str,
) -> dict:
    """Anthropic Claude Messages API 多輪 tool_use 迴圈(Sprint 9.1 per-user / 9.2 abort)。"""
    from app.services.ai_test_gen import _default_base_url, _default_model
    if not token.api_key:
        raise RuntimeError("Anthropic 需要 api_key")
    base_url = (token.base_url or _default_base_url(token.provider)).rstrip("/")
    model = token.model or _default_model(token.provider)
    headers = {
        "Content-Type": "application/json",
        "x-api-key": token.api_key,
        "anthropic-version": "2023-06-01",
    }

    user_intro = f"# 測試需求\n{prompt}"
    if target_url:
        user_intro += f"\n\n# 起始 URL\n{target_url}\n\n請用 tools 開頁並探索。"

    messages: list[dict] = [{"role": "user", "content": user_intro}]
    tools_schema = _mcp_tools_to_anthropic_schema(mcp_tools)
    tool_calls_log: list[dict] = []
    final_text: Optional[str] = None
    finish_reason: Optional[str] = None
    last_turn = 0

    async with httpx.AsyncClient(timeout=120.0) as client:
        for turn in range(max_turns):
            last_turn = turn + 1
            # Sprint 9.2 — abort 檢查
            if _mcp_run_abort_flags.get(username):
                _mcp_run_abort_flags.pop(username, None)
                final_text = "(使用者取消)"
                finish_reason = "aborted"
                break
            payload = {
                "model": model,
                "max_tokens": 4000,
                "system": _MCP_LOOP_SYSTEM_PROMPT,
                "messages": messages,
                "temperature": 0.2,
            }
            if tools_schema:
                payload["tools"] = tools_schema

            try:
                r = await client.post(base_url + "/v1/messages", headers=headers, json=payload)
                r.raise_for_status()
                resp = r.json()
            except Exception as e:
                raise RuntimeError(f"Anthropic 呼叫失敗(turn {turn+1}):{e}")

            stop_reason = resp.get("stop_reason")
            content_blocks = resp.get("content") or []
            finish_reason = stop_reason

            # 收集這輪 LLM 的 text / tool_use
            text_parts = [b.get("text", "") for b in content_blocks if isinstance(b, dict) and b.get("type") == "text"]
            tool_uses = [b for b in content_blocks if isinstance(b, dict) and b.get("type") == "tool_use"]

            if stop_reason != "tool_use" or not tool_uses:
                # LLM 結束(end_turn / max_tokens / stop_sequence)
                final_text = "\n".join(text_parts)
                break

            # 把這輪 assistant content(整個 array,含 tool_use)加進 history
            messages.append({"role": "assistant", "content": content_blocks})

            # 對每個 tool_use 呼叫 MCP,組 tool_result blocks
            tool_results = []
            for tu in tool_uses:
                tool_name = tu.get("name") or ""
                args = tu.get("input") or {}
                try:
                    rpc = await _mcp_jsonrpc("tools/call", {"name": tool_name, "arguments": args}, username=username, timeout=45)
                    if "error" in rpc:
                        result_text = json.dumps({"error": str(rpc["error"])}, ensure_ascii=False)
                    else:
                        rr = rpc.get("result") or {}
                        if isinstance(rr, dict):
                            text_p = []
                            for c in (rr.get("content") or []):
                                if isinstance(c, dict) and c.get("type") == "text":
                                    text_p.append(c.get("text") or "")
                            result_text = "\n".join(text_p) if text_p else json.dumps(rr, ensure_ascii=False)[:4000]
                        else:
                            result_text = str(rr)[:4000]
                except Exception as e:
                    result_text = json.dumps({"error": f"MCP exception: {e}"}, ensure_ascii=False)

                tool_calls_log.append({
                    "turn": turn + 1, "tool": tool_name, "arguments": args,
                    "result_preview": result_text[:300],
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.get("id") or "",
                    "content": result_text,
                })
            messages.append({"role": "user", "content": tool_results})
        else:
            # 跑滿 max_turns 沒結束 → 強制要 step
            messages.append({"role": "user", "content": "請立即停止 tool 呼叫,直接輸出 GeneratedStep JSON 陣列(沒圍欄沒解釋)。"})
            try:
                r = await client.post(
                    base_url + "/v1/messages", headers=headers,
                    json={"model": model, "max_tokens": 4000, "system": _MCP_LOOP_SYSTEM_PROMPT, "messages": messages, "temperature": 0.2},
                )
                r.raise_for_status()
                resp = r.json()
                final_text = "".join(b.get("text", "") for b in (resp.get("content") or []) if b.get("type") == "text")
                finish_reason = "stop_forced"
            except Exception as e:
                final_text = ""
                finish_reason = f"error_after_max_turns:{e}"

    steps = _extract_steps_from_text(final_text or "")
    return {
        "ok": bool(steps), "turns": last_turn, "finish_reason": finish_reason,
        "final_text": (final_text or "")[:4000], "steps_json": steps, "tool_calls_log": tool_calls_log,
    }


# ─── Sprint 8.b — Google Gemini function calling loop ─────────────────
def _mcp_tools_to_google_schema(mcp_tools: list[dict]) -> list[dict]:
    """Google generateContent tools 格式:[{functionDeclarations: [...]}]。"""
    fdecls = []
    for t in mcp_tools or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        params = t.get("input_schema") or t.get("inputSchema") or {"type": "object", "properties": {}}
        fdecls.append({
            "name": t["name"],
            "description": (t.get("description") or "")[:1024],
            "parameters": params,
        })
    return [{"functionDeclarations": fdecls}] if fdecls else []


async def _run_mcp_loop_google(
    token, prompt: str, target_url: Optional[str], max_turns: int, mcp_tools: list[dict], username: str,
) -> dict:
    """Google Gemini generateContent function calling 多輪迴圈(Sprint 9.1 per-user / 9.2 abort)。"""
    from app.services.ai_test_gen import _default_base_url, _default_model
    if not token.api_key:
        raise RuntimeError("Google Gemini 需要 api_key")
    base_url = (token.base_url or _default_base_url(token.provider)).rstrip("/")
    model = token.model or _default_model(token.provider)
    headers = {"Content-Type": "application/json"}

    user_intro = f"# 測試需求\n{prompt}"
    if target_url:
        user_intro += f"\n\n# 起始 URL\n{target_url}\n\n請用 tools 開頁並探索。"

    contents: list[dict] = [{"role": "user", "parts": [{"text": user_intro}]}]
    tools_schema = _mcp_tools_to_google_schema(mcp_tools)
    tool_calls_log: list[dict] = []
    final_text: Optional[str] = None
    finish_reason: Optional[str] = None
    last_turn = 0

    async with httpx.AsyncClient(timeout=120.0) as client:
        for turn in range(max_turns):
            last_turn = turn + 1
            # Sprint 9.2 — abort 檢查
            if _mcp_run_abort_flags.get(username):
                _mcp_run_abort_flags.pop(username, None)
                final_text = "(使用者取消)"
                finish_reason = "aborted"
                break
            payload = {
                "system_instruction": {"parts": [{"text": _MCP_LOOP_SYSTEM_PROMPT}]},
                "contents": contents,
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4000},
            }
            if tools_schema:
                payload["tools"] = tools_schema

            url = base_url + f"/models/{model}:generateContent?key={token.api_key}"
            try:
                r = await client.post(url, headers=headers, json=payload)
                r.raise_for_status()
                resp = r.json()
            except Exception as e:
                raise RuntimeError(f"Google 呼叫失敗(turn {turn+1}):{e}")

            candidates = resp.get("candidates") or []
            if not candidates:
                final_text = ""
                finish_reason = "no_candidates"
                break

            cand0 = candidates[0]
            cand_content = cand0.get("content") or {}
            parts = cand_content.get("parts") or []
            cand_finish = cand0.get("finishReason")
            finish_reason = cand_finish

            # 抓 functionCall + text
            fcalls = [p.get("functionCall") for p in parts if isinstance(p, dict) and p.get("functionCall")]
            texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]

            if not fcalls:
                final_text = "\n".join(texts)
                break

            # 記下 model 這輪的整段 content(放回 contents 維持 history)
            contents.append({"role": "model", "parts": parts})

            # 對每個 functionCall 呼叫 MCP,組 functionResponse parts
            response_parts = []
            for fc in fcalls:
                tool_name = fc.get("name") or ""
                args = fc.get("args") or {}
                try:
                    rpc = await _mcp_jsonrpc("tools/call", {"name": tool_name, "arguments": args}, username=username, timeout=45)
                    if "error" in rpc:
                        result_text = json.dumps({"error": str(rpc["error"])}, ensure_ascii=False)
                    else:
                        rr = rpc.get("result") or {}
                        if isinstance(rr, dict):
                            text_p = []
                            for c in (rr.get("content") or []):
                                if isinstance(c, dict) and c.get("type") == "text":
                                    text_p.append(c.get("text") or "")
                            result_text = "\n".join(text_p) if text_p else json.dumps(rr, ensure_ascii=False)[:4000]
                        else:
                            result_text = str(rr)[:4000]
                except Exception as e:
                    result_text = json.dumps({"error": f"MCP exception: {e}"}, ensure_ascii=False)

                tool_calls_log.append({
                    "turn": turn + 1, "tool": tool_name, "arguments": args,
                    "result_preview": result_text[:300],
                })
                # Gemini functionResponse content 欄位是 dict;包成 {"result": "..."} 即可
                response_parts.append({
                    "functionResponse": {
                        "name": tool_name,
                        "response": {"result": result_text},
                    }
                })
            contents.append({"role": "user", "parts": response_parts})
        else:
            # 跑滿 max_turns 強制要 step
            contents.append({"role": "user", "parts": [{"text": "請立即停止 functionCall,直接輸出 GeneratedStep JSON 陣列(沒圍欄沒解釋)。"}]})
            try:
                r = await client.post(
                    base_url + f"/models/{model}:generateContent?key={token.api_key}",
                    headers=headers,
                    json={
                        "system_instruction": {"parts": [{"text": _MCP_LOOP_SYSTEM_PROMPT}]},
                        "contents": contents,
                        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4000},
                    },
                )
                r.raise_for_status()
                resp = r.json()
                cand0 = (resp.get("candidates") or [{}])[0]
                final_text = "".join(p.get("text", "") for p in (cand0.get("content") or {}).get("parts") or [] if p.get("text"))
                finish_reason = "stop_forced"
            except Exception as e:
                final_text = ""
                finish_reason = f"error_after_max_turns:{e}"

    steps = _extract_steps_from_text(final_text or "")
    return {
        "ok": bool(steps), "turns": last_turn, "finish_reason": finish_reason,
        "final_text": (final_text or "")[:4000], "steps_json": steps, "tool_calls_log": tool_calls_log,
    }


@router.post("/ai/mcp/run", response_model=McpRunResponse, tags=["V · AI"])
@limiter.limit("10/hour")  # MCP 流量 + tokens 都很重,限速嚴一點
async def mcp_run(
    request: Request,
    payload: McpRunRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Sprint 7 — LLM 透過 MCP 自動探索 + 產 steps_json。
    流程: 確保 MCP 啟動 → 抓 tools → OpenAI tool-calling loop → 解析 LLM 最終輸出。

    僅支援 OpenAI / OpenAI-compatible provider(Local Ollama / DeepSeek 也可,
    需要該模型支援 function calling);Anthropic / Google 留作後續。
    """
    if not (payload.prompt or "").strip():
        raise HTTPException(400, "prompt 不能為空")
    state = _get_user_mcp_state(user.username)
    if not state.get("container_name"):
        raise HTTPException(409, "MCP server 沒在跑;請先 POST /api/ai/mcp/start(或從 設定 → AI Token → MCP PoC 啟動)")

    from app.services.ai_test_gen import pick_token
    from app.models.ai_token_config import AiProvider
    try:
        token = await pick_token(db, preferred_provider=payload.provider, organization_id=user.organization_id)
    except RuntimeError as e:
        raise HTTPException(400, str(e))

    # Sprint 8 — 三 provider 全支援(OpenAI / Anthropic / Google)
    # 抓 MCP tools(共用一次)
    try:
        rpc = await _mcp_jsonrpc("tools/list", {}, username=user.username)
    except Exception as e:
        raise HTTPException(502, f"MCP tools/list 失敗:{e}")
    if "error" in rpc:
        raise HTTPException(502, f"MCP error:{rpc['error']}")
    mcp_tools = (rpc.get("result") or {}).get("tools") or []
    if not mcp_tools:
        raise HTTPException(502, "MCP server 沒回任何 tool;確認 mcp 容器已 ready")

    # Sprint 9.2 — reset abort flag(若上次留下)
    _mcp_run_abort_flags.pop(user.username, None)
    _touch_mcp(user.username)  # Sprint 10.1 — run 開始算活動

    max_turns = max(1, min(int(payload.max_turns or 10), 30))
    if token.provider == AiProvider.ANTHROPIC:
        coro = _run_mcp_loop_anthropic(token, payload.prompt, payload.target_url, max_turns, mcp_tools, user.username)
    elif token.provider == AiProvider.GOOGLE:
        coro = _run_mcp_loop_google(token, payload.prompt, payload.target_url, max_turns, mcp_tools, user.username)
    else:
        coro = _run_mcp_loop(token, payload.prompt, payload.target_url, max_turns, mcp_tools, user.username)

    # Sprint 10.2 — 包成 asyncio Task,讓 /run-abort 能 task.cancel() 即時中止
    task = asyncio.create_task(coro)
    _mcp_run_tasks[user.username] = task
    try:
        result = await task
    except asyncio.CancelledError:
        # /run-abort 主動取消 → 回部分結果(空 steps)讓前端知道
        result = {
            "ok": False, "turns": 0, "finish_reason": "cancelled",
            "final_text": "(使用者即時取消,LLM 中斷)",
            "steps_json": [], "tool_calls_log": [],
        }
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        log.exception("mcp_run failed")
        raise HTTPException(502, f"MCP loop 例外:{e}")
    finally:
        _mcp_run_abort_flags.pop(user.username, None)
        _mcp_run_tasks.pop(user.username, None)
    return McpRunResponse(**result)


@router.post("/ai/mcp/run-abort", status_code=204, tags=["V · AI"])
async def mcp_run_abort(user: User = Depends(get_current_user)):
    """Sprint 9.2 + 10.2 — 取消當前 user 正在跑的 LLM ↔ MCP loop。
    雙保險:flag 給 loop 內 turn 切換時 break,task.cancel() 即時中斷 httpx。
    """
    _mcp_run_abort_flags[user.username] = True
    task = _mcp_run_tasks.get(user.username)
    if task and not task.done():
        task.cancel()


@router.get("/ai/mcp/status", response_model=McpStatus, tags=["V · AI"])
async def mcp_status(user: User = Depends(get_current_user)):
    state = _get_user_mcp_state(user.username)
    if not state.get("container_id"):
        return McpStatus(running=False)
    try:
        client = _get_mcp_docker()
        c = client.containers.get(state["container_id"])
        if c.status not in ("running", "created"):
            state.clear()
            return McpStatus(running=False)
    except Exception:
        state.clear()
        return McpStatus(running=False)
    return McpStatus(
        running=True,
        container_id=state["container_id"],
        container_name=state.get("container_name"),
        sse_port=state.get("sse_port"),
        sse_url=f"/mcp/sse?port={state.get('sse_port')}",
        started_at=state.get("started_at"),
    )
