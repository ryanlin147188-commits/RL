"""Entity Versions REST endpoints — 通用版本歷史 + 回滾 API。

  GET  /api/entity-versions/{entity_type}/{entity_id}
       → list of versions (any logged-in user)

  POST /api/entity-versions/{entity_type}/{entity_id}/revert
       body: {"version_id": "...", "reason": "..."}
       → admin / superuser only
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.user import User
from app.services import entity_version_service as evs

router = APIRouter()


class RevertRequest(BaseModel):
    version_id: str
    reason: Optional[str] = None



def _check_admin(user: User) -> None:
    """Revert / Approve / Reject 都是受權限控管的決策操作,限定 superuser。"""
    if not user.is_superuser:
        raise HTTPException(403, "需要 superuser 權限才能執行此操作")


@router.get(
    "/entity-versions/{entity_type}/{entity_id}",
    tags=["AC · 版本歷史"],
)
async def list_entity_versions(
    entity_type: str,
    entity_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出某 entity 的版本歷史(由新到舊)。

    多租戶 scope:非 superuser 只看得到 organization_id == 自己 org 的快照。
    """
    if not evs.is_known_entity_type(entity_type):
        raise HTTPException(400, f"未知的 entity_type:{entity_type}")
    org_filter = None if user.is_superuser else user.organization_id
    try:
        return await evs.list_versions(
            db,
            entity_type=entity_type,
            entity_id=entity_id,
            organization_id=org_filter,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post(
    "/entity-versions/{entity_type}/{entity_id}/revert",
    tags=["AC · 版本歷史"],
)
async def revert_entity_version(
    entity_type: str,
    entity_id: str,
    payload: RevertRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """把 entity 還原到指定版本。會在歷史鏈上建一筆 source='revert' 的新版本,
    parent_version_id 指向被還原的目標版本。"""
    _check_admin(user)
    if not evs.is_known_entity_type(entity_type):
        raise HTTPException(400, f"未知的 entity_type:{entity_type}")
    try:
        result = await evs.revert_to(
            db,
            entity_type=entity_type,
            entity_id=entity_id,
            target_version_id=payload.version_id,
            by=user.username,
            reason=payload.reason,
        )
        return result
    except ValueError as e:
        raise HTTPException(404, str(e))


