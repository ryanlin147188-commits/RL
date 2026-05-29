"""Skill 管理 + active skill 切換 REST endpoints — Phase 2a。

權限:
* list / set-active session.active_skill_id: 只要 ``get_current_user``,任何
  使用者可以讀清單與切自己 session 的 skill(不是 destructive 操作)。
* create / update / delete / import: ``SETTINGS_WRITE``,呼應其他 per-org
  settings 的權限模型。
* IDOR 防護:所有 mutation 都比對 ``user.organization_id`` vs path 上的
  ``org_id``。Superuser bypass 沿用既有慣例。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Path, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.permissions import require_casbin
from app.auth.permissions_catalog import P
from app.database import get_db
from app.models.agent_session import AgentSession
from app.models.skill import Skill
from app.models.user import User
from app.services import skill_service

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────


class SkillResponse(BaseModel):
    id: str
    organization_id: str
    name: str
    description: str
    trigger_keywords: list[str]
    system_prompt_addition: str
    allowed_tools: Optional[list[str]] = None
    mode_scope: list[str]
    enabled: bool
    version: int
    created_by: Optional[str] = None
    created_at: Any
    updated_at: Any

    class Config:
        from_attributes = True


class SkillCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str = ""
    system_prompt_addition: str = ""
    trigger_keywords: list[str] = []
    allowed_tools: Optional[list[str]] = None
    mode_scope: list[str] = []
    enabled: bool = True


class SkillUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=64)
    description: Optional[str] = None
    system_prompt_addition: Optional[str] = None
    trigger_keywords: Optional[list[str]] = None
    allowed_tools: Optional[list[str]] = None
    mode_scope: Optional[list[str]] = None
    enabled: Optional[bool] = None


class SetActiveSkillRequest(BaseModel):
    skill_id: Optional[str] = None  # None / "" = 取消 active


# ── Helpers ──────────────────────────────────────────────────────────


def _check_org_access(user: User, org_id: str) -> None:
    """非 superuser 只能存取自己 org 的 skills。"""
    if user.is_superuser:
        return
    if user.organization_id != org_id:
        raise HTTPException(403, "無權存取此組織的 skills")


def _to_response(row: Skill) -> dict:
    return {
        "id": row.id,
        "organization_id": row.organization_id,
        "name": row.name,
        "description": row.description,
        "trigger_keywords": row.trigger_keywords or [],
        "system_prompt_addition": row.system_prompt_addition,
        "allowed_tools": row.allowed_tools,
        "mode_scope": row.mode_scope or [],
        "enabled": row.enabled,
        "version": row.version,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _map_skill_error(exc: skill_service.SkillError) -> HTTPException:
    if isinstance(exc, skill_service.SkillNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, skill_service.SkillNameConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, skill_service.SkillMarkdownInvalid):
        return HTTPException(422, str(exc))
    return HTTPException(400, str(exc))


# ── Endpoints: per-org CRUD ──────────────────────────────────────────


@router.get(
    "/v1/orgs/{org_id}/skills",
    response_model=list[SkillResponse],
    tags=["AE · Agent"],
)
async def list_skills(
    org_id: str = Path(...),
    mode: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _check_org_access(user, org_id)
    rows = await skill_service.list_skills(
        db, organization_id=org_id, mode=mode, enabled_only=False
    )
    return [_to_response(r) for r in rows]


@router.post(
    "/v1/orgs/{org_id}/skills",
    response_model=SkillResponse,
    tags=["AE · Agent"],
    dependencies=[Depends(require_casbin(P.SETTINGS_WRITE))],
)
async def create_skill(
    payload: SkillCreate,
    org_id: str = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _check_org_access(user, org_id)
    try:
        row = await skill_service.create_skill(
            db,
            organization_id=org_id,
            created_by=user.id,
            name=payload.name,
            description=payload.description,
            system_prompt_addition=payload.system_prompt_addition,
            trigger_keywords=payload.trigger_keywords,
            allowed_tools=payload.allowed_tools,
            mode_scope=payload.mode_scope,
            enabled=payload.enabled,
        )
    except skill_service.SkillError as exc:
        raise _map_skill_error(exc) from exc
    return _to_response(row)


@router.put(
    "/v1/orgs/{org_id}/skills/{skill_id}",
    response_model=SkillResponse,
    tags=["AE · Agent"],
    dependencies=[Depends(require_casbin(P.SETTINGS_WRITE))],
)
async def update_skill(
    payload: SkillUpdate,
    org_id: str = Path(...),
    skill_id: str = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _check_org_access(user, org_id)
    # Pydantic 沒設的欄位用 model_dump exclude_unset 過濾 → service 才知道哪些要動
    body = payload.model_dump(exclude_unset=True)
    try:
        row = await skill_service.update_skill(
            db, skill_id=skill_id, organization_id=org_id, payload=body
        )
    except skill_service.SkillError as exc:
        raise _map_skill_error(exc) from exc
    return _to_response(row)


@router.delete(
    "/v1/orgs/{org_id}/skills/{skill_id}",
    status_code=204,
    tags=["AE · Agent"],
    dependencies=[Depends(require_casbin(P.SETTINGS_WRITE))],
)
async def delete_skill(
    org_id: str = Path(...),
    skill_id: str = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _check_org_access(user, org_id)
    try:
        await skill_service.delete_skill(
            db, skill_id=skill_id, organization_id=org_id
        )
    except skill_service.SkillError as exc:
        raise _map_skill_error(exc) from exc
    return None


@router.post(
    "/v1/orgs/{org_id}/skills/import",
    response_model=SkillResponse,
    tags=["AE · Agent"],
    dependencies=[Depends(require_casbin(P.SETTINGS_WRITE))],
)
async def import_skill_from_markdown(
    org_id: str = Path(...),
    file: UploadFile = File(...),
    overwrite: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """上傳 ``SKILL.md`` 風格的檔案(YAML frontmatter + body)建立 / 覆寫 skill。"""
    _check_org_access(user, org_id)
    # 限 256KB(skill body 一般幾 KB;設個安全上限避免大檔 DoS)
    raw = await file.read()
    if len(raw) > 256 * 1024:
        raise HTTPException(413, "檔案過大(上限 256KB)")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, f"檔案非 UTF-8 編碼: {exc}") from exc
    try:
        row = await skill_service.import_from_markdown(
            db,
            organization_id=org_id,
            created_by=user.id,
            content=content,
            overwrite=overwrite,
        )
    except skill_service.SkillError as exc:
        raise _map_skill_error(exc) from exc
    return _to_response(row)


# ── Endpoints: set-active on a session ───────────────────────────────


@router.post(
    "/v1/agent/sessions/{session_id}/skill",
    tags=["AE · Agent"],
)
async def set_active_skill(
    payload: SetActiveSkillRequest,
    session_id: str = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """切換 session 的 active skill。``skill_id=null`` 取消啟用。"""
    # session 必須屬於此 user(沿用 agent.router 的 IDOR pattern)
    stmt = select(AgentSession).where(AgentSession.id == session_id)
    session = (await db.execute(stmt)).scalar_one_or_none()
    if session is None:
        raise HTTPException(404, "session 不存在")
    if not user.is_superuser and session.user_id != user.id:
        raise HTTPException(403, "無權修改他人 session")

    if payload.skill_id:
        # 驗證 skill 屬於同一 org 且 enabled
        if not session.organization_id:
            raise HTTPException(400, "session 無 organization,無法套用 skill")
        try:
            skill = await skill_service.get_skill(
                db,
                skill_id=payload.skill_id,
                organization_id=session.organization_id,
            )
        except skill_service.SkillNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        if not skill.enabled:
            raise HTTPException(409, "該 skill 已停用")
        session.active_skill_id = skill.id
    else:
        session.active_skill_id = None

    await db.flush()
    return {
        "session_id": session.id,
        "active_skill_id": session.active_skill_id,
    }
