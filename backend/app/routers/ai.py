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
