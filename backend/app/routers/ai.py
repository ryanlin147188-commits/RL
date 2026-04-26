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
from app.services.ai_test_gen import generate_testcases_from_requirement

router = APIRouter()


class AiGenerateRequest(BaseModel):
    n: int = 3
    provider: Optional[str] = None  # 不指定 → 用系統預設


class AiGeneratedItem(BaseModel):
    title: str
    ac: str
    steps_md: str


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
