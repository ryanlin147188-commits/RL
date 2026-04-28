"""AI 相關 REST endpoints — 目前包含「從需求生成測試案例」MVP。

(同 auth.py：刻意不開啟 `from __future__ import annotations`，避免
與 slowapi `@limiter.limit` 的型別內省衝突。)
"""
import asyncio
import json
import logging
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


# 單例(同 _recorder_containers 模式但只允許一個)
_mcp_state: dict = {}


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
        api = docker.APIClient(base_url="unix:///var/run/docker.sock")
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
    """啟一個 Playwright MCP server 容器(若已有跑著的不重啟,直接回現狀)。"""
    if _mcp_state.get("container_id"):
        # 驗證仍 alive
        try:
            client = _get_mcp_docker()
            c = client.containers.get(_mcp_state["container_id"])
            if c.status in ("running", "created"):
                return McpStatus(
                    running=True,
                    container_id=_mcp_state["container_id"],
                    container_name=_mcp_state.get("container_name"),
                    sse_port=_mcp_state.get("sse_port"),
                    sse_url=f"/mcp/sse?port={_mcp_state.get('sse_port')}",
                    started_at=_mcp_state.get("started_at"),
                )
        except Exception:
            _mcp_state.clear()

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

    name = f"autotest-mcp-{secrets.token_hex(4)}"
    try:
        c = client.containers.run(
            image=_settings.MCP_IMAGE,
            name=name,
            detach=True,
            auto_remove=True,
            network=_settings.RECORDER_NETWORK,
            ports={"8931/tcp": None},
            labels={"autotest.role": "mcp"},
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
    _mcp_state.update({
        "container_id": c.id,
        "container_name": name,
        "sse_port": sse_port,
        "started_at": started_at,
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
    info = dict(_mcp_state)
    _mcp_state.clear()
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
    if not _mcp_state.get("container_id") or not _mcp_state.get("container_name"):
        return McpToolsResponse(running=False, error="MCP server 沒在跑;請先 POST /api/ai/mcp/start")

    # 從 backend 容器內走 internal docker network 連 MCP server(用 container_name 當 hostname)
    mcp_url = f"http://{_mcp_state['container_name']}:8931/mcp"
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
    if not _mcp_state.get("container_name"):
        raise HTTPException(409, "MCP server 沒在跑;請先 POST /api/ai/mcp/start")

    mcp_url = f"http://{_mcp_state['container_name']}:8931/mcp"
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


@router.get("/ai/mcp/status", response_model=McpStatus, tags=["V · AI"])
async def mcp_status(user: User = Depends(get_current_user)):
    if not _mcp_state.get("container_id"):
        return McpStatus(running=False)
    try:
        client = _get_mcp_docker()
        c = client.containers.get(_mcp_state["container_id"])
        if c.status not in ("running", "created"):
            _mcp_state.clear()
            return McpStatus(running=False)
    except Exception:
        _mcp_state.clear()
        return McpStatus(running=False)
    return McpStatus(
        running=True,
        container_id=_mcp_state["container_id"],
        container_name=_mcp_state.get("container_name"),
        sse_port=_mcp_state.get("sse_port"),
        sse_url=f"/mcp/sse?port={_mcp_state.get('sse_port')}",
        started_at=_mcp_state.get("started_at"),
    )
