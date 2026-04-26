"""Settings 相關 REST endpoints（Role / Notification / Email / AI Token）。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import asc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.ai_token_config import AiProvider, AiTokenConfig
from app.models.email_config import EmailConfig
from app.models.notification_preference import NotificationPreference
from app.models.role import Role
from app.schemas.settings import (
    AiTokenConfigCreate,
    AiTokenConfigResponse,
    AiTokenConfigUpdate,
    EmailConfigResponse,
    EmailConfigUpdate,
    NotificationPreferenceCreate,
    NotificationPreferenceResponse,
    NotificationPreferenceUpdate,
    RoleCreate,
    RoleResponse,
    RoleUpdate,
)

router = APIRouter()


# ─── Permission catalogue (固定字串清單；前端依此產生 checkbox) ─────────
_PERMISSION_CATALOGUE = [
    {"key": "project.read", "label": "查看專案", "group": "專案"},
    {"key": "project.write", "label": "編輯專案", "group": "專案"},
    {"key": "project.delete", "label": "刪除專案", "group": "專案"},
    {"key": "testcase.read", "label": "查看測試案例", "group": "測試案例"},
    {"key": "testcase.write", "label": "編輯測試案例", "group": "測試案例"},
    {"key": "testcase.delete", "label": "刪除測試案例", "group": "測試案例"},
    {"key": "testcase.execute", "label": "執行測試", "group": "測試案例"},
    {"key": "defect.read", "label": "查看缺陷", "group": "缺陷"},
    {"key": "defect.write", "label": "編輯缺陷", "group": "缺陷"},
    {"key": "defect.delete", "label": "刪除缺陷", "group": "缺陷"},
    {"key": "requirement.read", "label": "查看需求", "group": "需求"},
    {"key": "requirement.write", "label": "編輯需求", "group": "需求"},
    {"key": "requirement.delete", "label": "刪除需求", "group": "需求"},
    {"key": "plan.read", "label": "查看測試計畫", "group": "測試計畫"},
    {"key": "plan.write", "label": "編輯測試計畫", "group": "測試計畫"},
    {"key": "plan.approve", "label": "核准測試計畫", "group": "測試計畫"},
    {"key": "wbs.read", "label": "查看 WBS", "group": "WBS"},
    {"key": "wbs.write", "label": "編輯 WBS", "group": "WBS"},
    {"key": "document.read", "label": "查看文件", "group": "文件"},
    {"key": "document.write", "label": "編輯文件", "group": "文件"},
    {"key": "report.read", "label": "查看報告", "group": "報告"},
    {"key": "settings.read", "label": "查看設定", "group": "設定"},
    {"key": "settings.write", "label": "修改設定", "group": "設定"},
    {"key": "user.manage", "label": "管理使用者", "group": "使用者"},
    {"key": "role.manage", "label": "管理角色", "group": "使用者"},
]

_NOTIFICATION_EVENT_CATALOGUE = [
    {"key": "defect.created", "label": "新增缺陷", "group": "缺陷"},
    {"key": "defect.assigned", "label": "缺陷被指派", "group": "缺陷"},
    {"key": "defect.status_changed", "label": "缺陷狀態變更", "group": "缺陷"},
    {"key": "run.started", "label": "測試開始執行", "group": "執行"},
    {"key": "run.failed", "label": "測試執行失敗", "group": "執行"},
    {"key": "run.passed", "label": "測試執行通過", "group": "執行"},
    {"key": "schedule.fired", "label": "排程觸發", "group": "排程"},
    {"key": "milestone.due_soon", "label": "里程碑即將到期", "group": "時程"},
    {"key": "todo.due_soon", "label": "待辦即將到期", "group": "待辦"},
    {"key": "plan.approved", "label": "測試計畫被核准", "group": "計畫"},
    {"key": "requirement.changed", "label": "需求異動", "group": "需求"},
]


@router.get("/settings/permissions/catalogue", tags=["S · 設定"])
async def list_permission_catalogue():
    """前端建構角色 checkbox 用：所有可指派的權限 key 與顯示名稱。"""
    return {"items": _PERMISSION_CATALOGUE}


@router.get("/settings/notifications/catalogue", tags=["S · 設定"])
async def list_notification_catalogue():
    """前端建構通知設定 grid 用：所有可訂閱的事件。"""
    return {"items": _NOTIFICATION_EVENT_CATALOGUE}


# ─── Role CRUD ─────────────────────────────────────────────────────────

@router.get("/settings/roles", response_model=list[RoleResponse], tags=["S · 設定"])
async def list_roles(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Role).order_by(asc(Role.name)))).scalars().all()
    return list(rows)


@router.post(
    "/settings/roles", response_model=RoleResponse, status_code=201, tags=["S · 設定"]
)
async def create_role(payload: RoleCreate, db: AsyncSession = Depends(get_db)):
    # 確保名稱唯一
    existing = (await db.execute(select(Role).where(Role.name == payload.name))).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"角色名稱「{payload.name}」已存在")
    role = Role(
        name=payload.name,
        description=payload.description,
        permissions_json=list(payload.permissions_json or []),
        is_system=False,
    )
    db.add(role)
    await db.flush()
    await db.refresh(role)
    return role


@router.put(
    "/settings/roles/{role_id}", response_model=RoleResponse, tags=["S · 設定"]
)
async def update_role(role_id: str, payload: RoleUpdate, db: AsyncSession = Depends(get_db)):
    r = await db.get(Role, role_id)
    if not r:
        raise HTTPException(404, "Role not found")
    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"] and data["name"] != r.name:
        if r.is_system:
            raise HTTPException(400, "系統角色名稱不可修改")
        dup = (await db.execute(select(Role).where(Role.name == data["name"]))).scalar_one_or_none()
        if dup:
            raise HTTPException(409, f"角色名稱「{data['name']}」已存在")
    for k, v in data.items():
        if v is not None:
            setattr(r, k, v)
    await db.flush()
    await db.refresh(r)
    return r


@router.delete("/settings/roles/{role_id}", status_code=204, tags=["S · 設定"])
async def delete_role(role_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.get(Role, role_id)
    if not r:
        raise HTTPException(404, "Role not found")
    if r.is_system:
        raise HTTPException(400, "系統角色不可刪除")
    await db.delete(r)
    await db.flush()


# ─── NotificationPreference ────────────────────────────────────────────

@router.get(
    "/settings/notifications",
    response_model=list[NotificationPreferenceResponse],
    tags=["S · 設定"],
)
async def list_notification_prefs(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(NotificationPreference))).scalars().all()
    return list(rows)


@router.get(
    "/settings/notifications/by-username/{username}",
    response_model=NotificationPreferenceResponse,
    tags=["S · 設定"],
)
async def get_notification_pref(username: str, db: AsyncSession = Depends(get_db)):
    pref = (
        await db.execute(select(NotificationPreference).where(NotificationPreference.username == username))
    ).scalar_one_or_none()
    if not pref:
        # 自動建立空的設定，方便前端直接編輯
        pref = NotificationPreference(username=username, events_json={})
        db.add(pref)
        await db.flush()
        await db.refresh(pref)
    return pref


@router.put(
    "/settings/notifications/by-username/{username}",
    response_model=NotificationPreferenceResponse,
    tags=["S · 設定"],
)
async def update_notification_pref(
    username: str, payload: NotificationPreferenceUpdate, db: AsyncSession = Depends(get_db)
):
    pref = (
        await db.execute(select(NotificationPreference).where(NotificationPreference.username == username))
    ).scalar_one_or_none()
    if not pref:
        pref = NotificationPreference(username=username, events_json=payload.events_json or {})
        db.add(pref)
    else:
        if payload.events_json is not None:
            pref.events_json = payload.events_json
    await db.flush()
    await db.refresh(pref)
    return pref


# ─── EmailConfig ──────────────────────────────────────────────────────

@router.get("/settings/email", response_model=EmailConfigResponse, tags=["S · 設定"])
async def get_email_config(db: AsyncSession = Depends(get_db)):
    cfg = await db.get(EmailConfig, "default")
    if not cfg:
        cfg = EmailConfig(id="default")
        db.add(cfg)
        await db.flush()
        await db.refresh(cfg)
    return cfg


@router.put("/settings/email", response_model=EmailConfigResponse, tags=["S · 設定"])
async def update_email_config(payload: EmailConfigUpdate, db: AsyncSession = Depends(get_db)):
    cfg = await db.get(EmailConfig, "default")
    if not cfg:
        cfg = EmailConfig(id="default")
        db.add(cfg)
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(cfg, k, v)
    await db.flush()
    await db.refresh(cfg)
    return cfg


# ─── AiTokenConfig ────────────────────────────────────────────────────

@router.get(
    "/settings/ai-tokens", response_model=list[AiTokenConfigResponse], tags=["S · 設定"]
)
async def list_ai_tokens(
    provider: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AiTokenConfig).order_by(asc(AiTokenConfig.provider), asc(AiTokenConfig.name))
    if provider:
        stmt = stmt.where(AiTokenConfig.provider == AiProvider(provider))
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.post(
    "/settings/ai-tokens",
    response_model=AiTokenConfigResponse,
    status_code=201,
    tags=["S · 設定"],
)
async def create_ai_token(payload: AiTokenConfigCreate, db: AsyncSession = Depends(get_db)):
    try:
        provider_enum = AiProvider(payload.provider)
    except ValueError:
        raise HTTPException(400, f"未知的 provider：{payload.provider}")
    token = AiTokenConfig(
        name=payload.name,
        provider=provider_enum,
        api_key=payload.api_key,
        base_url=payload.base_url,
        model=payload.model,
        enabled=payload.enabled,
        is_default=payload.is_default,
        description=payload.description,
    )
    db.add(token)
    await db.flush()
    if token.is_default:
        # 同 provider 內只能有一個 default
        await db.execute(
            update(AiTokenConfig)
            .where(AiTokenConfig.provider == provider_enum, AiTokenConfig.id != token.id)
            .values(is_default=False)
        )
    await db.flush()
    await db.refresh(token)
    return token


@router.put(
    "/settings/ai-tokens/{token_id}",
    response_model=AiTokenConfigResponse,
    tags=["S · 設定"],
)
async def update_ai_token(
    token_id: str, payload: AiTokenConfigUpdate, db: AsyncSession = Depends(get_db)
):
    t = await db.get(AiTokenConfig, token_id)
    if not t:
        raise HTTPException(404, "AI token not found")
    data = payload.model_dump(exclude_unset=True)
    if "provider" in data and data["provider"] is not None:
        try:
            data["provider"] = AiProvider(data["provider"])
        except ValueError:
            raise HTTPException(400, f"未知的 provider：{data['provider']}")
    for k, v in data.items():
        setattr(t, k, v)
    await db.flush()
    if t.is_default:
        await db.execute(
            update(AiTokenConfig)
            .where(AiTokenConfig.provider == t.provider, AiTokenConfig.id != t.id)
            .values(is_default=False)
        )
        await db.flush()
    await db.refresh(t)
    return t


@router.delete(
    "/settings/ai-tokens/{token_id}", status_code=204, tags=["S · 設定"]
)
async def delete_ai_token(token_id: str, db: AsyncSession = Depends(get_db)):
    t = await db.get(AiTokenConfig, token_id)
    if not t:
        raise HTTPException(404, "AI token not found")
    await db.delete(t)
    await db.flush()
