"""AI 相關 REST endpoints — 目前包含「從需求生成測試案例」MVP。

(同 auth.py：刻意不開啟 `from __future__ import annotations`，避免
與 slowapi `@limiter.limit` 的型別內省衝突。)
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
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
        raise HTTPException(
            425,
            detail={
                "code": "mcp_image_missing",
                "message": (
                    f"找不到 image `{_settings.MCP_IMAGE}`;"
                    "請跑 `./deploy.sh` 或手動 build:`docker build -f backend/Dockerfile.mcp "
                    "-t autotest-mcp:latest backend/`"
                ),
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
