"""Settings 相關 REST endpoints（Role / Notification / Email / AI Token）。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import asc, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.permissions import require_casbin
from app.auth.permissions_catalog import P
from app.database import get_db
from app.models.email_config import EmailConfig
from app.models.notification_preference import NotificationPreference
from app.models.org_membership import OrgMembership
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.role import Role
from app.models.user import User
from app.schemas.settings import (
    EmailConfigResponse,
    EmailConfigUpdate,
    NotificationPreferenceResponse,
    NotificationPreferenceUpdate,
    RoleCreate,
    RoleResponse,
    RoleUpdate,
)

router = APIRouter()


def _email_to_response(cfg: EmailConfig) -> dict:
    return {
        "id": cfg.id,
        "smtp_host": cfg.smtp_host,
        "smtp_port": cfg.smtp_port,
        "smtp_user": cfg.smtp_user,
        "smtp_password": None,
        "has_smtp_password": bool(cfg.smtp_password),
        "use_tls": cfg.use_tls,
        "from_address": cfg.from_address,
        "from_name": cfg.from_name,
        "enabled": cfg.enabled,
        "updated_at": cfg.updated_at,
    }


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
    {"key": "review.manage", "label": "審核(通過 / 退回 / 指派)", "group": "審核"},
    {"key": "review.delete", "label": "刪除審核紀錄", "group": "審核"},
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
    {"key": "todo.assigned", "label": "待辦被指派", "group": "待辦"},
    {"key": "plan.approved", "label": "測試計畫被核准", "group": "計畫"},
    {"key": "requirement.changed", "label": "需求異動", "group": "需求"},
    # Phase 3 — review state-machine notifications fired by review_service
    {"key": "review.submitted", "label": "送審通知(指派給您)", "group": "審核"},
    {"key": "review.approved", "label": "您送審的項目已通過", "group": "審核"},
    {"key": "review.rejected", "label": "您送審的項目被退回", "group": "審核"},
    {"key": "review.reverted", "label": "已通過/退回的項目被退回待審", "group": "審核"},
    # Phase 2 — generic assignment endpoint
    {"key": "assignment.received", "label": "您被指派一筆項目", "group": "指派"},
]


@router.get(
    "/settings/permissions/catalogue",
    tags=["S · 設定"],
    dependencies=[Depends(require_casbin(P.SETTINGS_READ))],
)
async def list_permission_catalogue():
    """前端建構角色 checkbox 用：所有可指派的權限 key 與顯示名稱。"""
    return {"items": _PERMISSION_CATALOGUE}


_PERMISSION_KEYS = {p["key"] for p in _PERMISSION_CATALOGUE}


@router.get(
    "/settings/permissions/{key}/usage",
    tags=["S · 設定"],
    dependencies=[Depends(require_casbin(P.SETTINGS_READ))],
)
async def get_permission_usage(
    key: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """C1 — 反向查詢:給定一個權限 key,回傳哪些角色含它 + 各自的使用者數。
    讓 admin 改 catalogue / 刪角色前能看到完整影響面。"""
    if key not in _PERMISSION_KEYS:
        raise HTTPException(404, f"未知的權限 key:{key}")
    role_stmt = _role_visibility_filter(select(Role), user)
    roles = (await db.execute(role_stmt)).scalars().all()
    matched = [r for r in roles if isinstance(r.permissions_json, list) and key in r.permissions_json]
    out_roles: list[dict] = []
    user_set: set[str] = set()
    for r in matched:
        org_users = (await db.execute(
            select(OrgMembership.username).where(OrgMembership.role_id == r.id)
        )).scalars().all()
        proj_users = (await db.execute(
            select(ProjectMember.username).where(ProjectMember.role_id == r.id)
        )).scalars().all()
        users_for_role = set(org_users) | set(proj_users)
        user_set |= users_for_role
        out_roles.append({
            "id": r.id,
            "name": r.name,
            "scope": r.scope,
            "is_system": bool(r.is_system),
            "users_count": len(users_for_role),
            "org_members_count": len(set(org_users)),
            "project_members_count": len(set(proj_users)),
        })
    out_roles.sort(key=lambda x: (-x["users_count"], x["name"]))
    return {
        "key": key,
        "roles": out_roles,
        "total_users": len(user_set),
    }


@router.get(
    "/settings/notifications/catalogue",
    tags=["S · 設定"],
    dependencies=[Depends(require_casbin(P.SETTINGS_READ))],
)
async def list_notification_catalogue():
    """前端建構通知設定 grid 用：所有可訂閱的事件。"""
    return {"items": _NOTIFICATION_EVENT_CATALOGUE}


# ─── Role CRUD（org-scoped） ──────────────────────────────────────────
# 同名 role 在不同 org 內可重複；使用者只看得到「自己 org 的 role」+「全域系統 role（org_id=NULL）」

def _role_visibility_filter(stmt, user: User):
    if user.is_superuser:
        return stmt
    return stmt.where(
        (Role.organization_id == user.organization_id)
        | (Role.organization_id.is_(None) & Role.is_system.is_(True))
    )


_ROLE_SORT_COLS = {
    "name": Role.name,
    "scope": Role.scope,
    "is_system": Role.is_system,
    "created_at": Role.created_at,
}


@router.get(
    "/settings/roles",
    response_model=list[RoleResponse],
    tags=["S · 設定"],
    dependencies=[Depends(require_casbin(P.SETTINGS_READ))],
)
async def list_roles(
    response: Response,
    search: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
    limit: Optional[int] = None,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出可見角色。可選 `?search=&sort_by=&sort_dir=&limit=&offset=`。
    帶 `limit` 時 response header 加 `X-Total-Count`。"""
    stmt = select(Role)
    stmt = _role_visibility_filter(stmt, user)

    if search:
        q = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Role.name).like(q),
                func.lower(func.coalesce(Role.description, "")).like(q),
            )
        )

    sort_col = _ROLE_SORT_COLS.get((sort_by or "").strip()) or Role.name
    direction = desc if (sort_dir or "asc").lower() == "desc" else asc
    stmt = stmt.order_by(direction(sort_col))

    if limit is not None:
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await db.execute(count_stmt)).scalar_one() or 0
        response.headers["X-Total-Count"] = str(total)
        try:
            limit_int = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            limit_int = 50
        try:
            offset_int = max(0, int(offset))
        except (TypeError, ValueError):
            offset_int = 0
        stmt = stmt.limit(limit_int).offset(offset_int)

    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.post(
    "/settings/roles",
    response_model=RoleResponse,
    status_code=201,
    tags=["S · 設定"],
    dependencies=[Depends(require_casbin(P.ROLE_MANAGE))],
)
async def create_role(
    payload: RoleCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # 同 org 內名稱不可重複
    existing = (
        await db.execute(
            select(Role).where(
                Role.name == payload.name,
                Role.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"角色名稱「{payload.name}」在本組織內已存在")
    role = Role(
        name=payload.name,
        organization_id=user.organization_id,
        description=payload.description,
        permissions_json=list(payload.permissions_json or []),
        is_system=False,
    )
    db.add(role)
    await db.flush()
    await db.refresh(role)
    return role


@router.put(
    "/settings/roles/{role_id}",
    response_model=RoleResponse,
    tags=["S · 設定"],
    dependencies=[Depends(require_casbin(P.ROLE_MANAGE))],
)
async def update_role(
    role_id: str,
    payload: RoleUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    r = await db.get(Role, role_id)
    if not r:
        raise HTTPException(404, "Role not found")
    # org 防護：非 superuser 只能改自己 org 的 role
    if not user.is_superuser and r.organization_id != user.organization_id:
        raise HTTPException(404, "Role not found")
    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"] and data["name"] != r.name:
        if r.is_system:
            raise HTTPException(400, "系統角色名稱不可修改")
        dup = (
            await db.execute(
                select(Role).where(
                    Role.name == data["name"], Role.organization_id == r.organization_id
                )
            )
        ).scalar_one_or_none()
        if dup:
            raise HTTPException(409, f"角色名稱「{data['name']}」已存在")
    for k, v in data.items():
        if v is not None:
            setattr(r, k, v)
    await db.flush()
    await db.refresh(r)
    # role.permissions_json / scope 改了會影響所有持有此 role 的 user 的
    # Casbin g + p 規則 → 全表 truncate-and-rewrite
    from app.auth.casbin_sync import schedule_full_resync
    schedule_full_resync()
    return r


@router.delete(
    "/settings/roles/{role_id}",
    status_code=204,
    tags=["S · 設定"],
    dependencies=[Depends(require_casbin(P.ROLE_MANAGE))],
)
async def delete_role(
    role_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    r = await db.get(Role, role_id)
    if not r:
        raise HTTPException(404, "Role not found")
    if not user.is_superuser and r.organization_id != user.organization_id:
        raise HTTPException(404, "Role not found")
    if r.is_system:
        raise HTTPException(400, "系統角色不可刪除")
    await db.delete(r)
    await db.flush()
    from app.auth.casbin_sync import schedule_full_resync
    schedule_full_resync()


# ─── Tier B2:角色使用數(讓 admin 看清能否安全刪除 / 改權限) ─────────
@router.get(
    "/settings/roles/{role_id}/usage",
    tags=["S · 設定"],
    dependencies=[Depends(require_casbin(P.SETTINGS_READ))],
)
async def get_role_usage(
    role_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """回傳該 role 在 OrgMembership / ProjectMember 各被多少人 / 多少專案使用,
    以及哪些 user 把這個 role 設成 OrgMembership.is_default(他們登入會切到對應 org)。
    讀取性 endpoint;權限走 SETTINGS_READ。"""
    r = await db.get(Role, role_id)
    if not r:
        raise HTTPException(404, "Role not found")
    # 跨 org 防護:非 superuser 只能查自己 org / 系統 role(org_id is null)
    if not user.is_superuser and r.organization_id and r.organization_id != user.organization_id:
        raise HTTPException(404, "Role not found")

    # OrgMembership 用此 role 的人數 + 多少人把這當 default
    org_members_count = (await db.execute(
        select(func.count()).select_from(OrgMembership).where(OrgMembership.role_id == role_id)
    )).scalar_one() or 0
    default_org_for_users = (await db.execute(
        select(func.count()).select_from(OrgMembership)
        .where(OrgMembership.role_id == role_id)
        .where(OrgMembership.is_default.is_(True))
    )).scalar_one() or 0

    # ProjectMember 用此 role 的人數 + 多少 project 有人在用
    project_members_count = (await db.execute(
        select(func.count()).select_from(ProjectMember).where(ProjectMember.role_id == role_id)
    )).scalar_one() or 0

    # users 表中 role_id = this role 的 user 數 — 給前端「使用人數」直接用
    from app.models.user import User
    user_count = (await db.execute(
        select(func.count()).select_from(User).where(User.role_id == role_id)
    )).scalar_one() or 0
    project_rows = (await db.execute(
        select(Project.id, Project.name, func.count(ProjectMember.id).label("count"))
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.role_id == role_id)
        .group_by(Project.id, Project.name)
        .order_by(func.count(ProjectMember.id).desc())
        .limit(50)
    )).all()
    return {
        "role_id": role_id,
        "role_name": r.name,
        "user_count": int(user_count),                # users 表直接掛此 role 的 user 數
        "org_members_count": int(org_members_count),
        "default_org_for_users": int(default_org_for_users),
        "project_members_count": int(project_members_count),
        "projects": [
            {"id": p.id, "name": p.name, "count": int(p.count)} for p in project_rows
        ],
        "total_users": int(org_members_count) + int(project_members_count),
    }


# ─── Tier B4:Clone role(快速建立相似權限的新角色) ────────────────
@router.post(
    "/settings/roles/{role_id}/clone",
    response_model=RoleResponse,
    status_code=201,
    tags=["S · 設定"],
    dependencies=[Depends(require_casbin(P.ROLE_MANAGE))],
)
async def clone_role(
    role_id: str,
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """從現有 role 拷貝 permissions_json 建立新 role。
    body: `{"new_name": "...", "description": "..."}`(description 可省;預設帶
    "(複製自 X)" 字樣)。新 role 強制 is_system=False、organization_id 跟
    呼叫者一樣(superuser 可填 organization_id 跨 org 建)。"""
    src = await db.get(Role, role_id)
    if not src:
        raise HTTPException(404, "Role not found")
    if not user.is_superuser and src.organization_id and src.organization_id != user.organization_id:
        raise HTTPException(404, "Role not found")

    new_name = ((payload or {}).get("new_name") or "").strip()
    if not new_name:
        raise HTTPException(400, "缺少 new_name")
    target_org_id = user.organization_id
    if user.is_superuser:
        target_org_id = (payload or {}).get("organization_id") or user.organization_id

    # 同 org 不可重名
    dup = (await db.execute(
        select(Role).where(Role.name == new_name, Role.organization_id == target_org_id)
    )).scalar_one_or_none()
    if dup:
        raise HTTPException(409, f"角色名稱「{new_name}」在本組織已存在")

    description = (payload or {}).get("description") or f"(複製自 {src.name})"
    cloned = Role(
        name=new_name,
        organization_id=target_org_id,
        description=description,
        permissions_json=list(src.permissions_json or []),
        is_system=False,
        scope=src.scope or "org",
    )
    db.add(cloned)
    await db.flush()
    await db.refresh(cloned)
    return cloned


# ─── NotificationPreference ────────────────────────────────────────────

@router.get(
    "/settings/notifications",
    response_model=list[NotificationPreferenceResponse],
    tags=["S · 設定"],
    dependencies=[Depends(require_casbin(P.SETTINGS_READ))],
)
async def list_notification_prefs(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(NotificationPreference)
    if not user.is_superuser:
        stmt = stmt.where(
            (NotificationPreference.username == user.username)
            | (NotificationPreference.username.is_(None))
        )
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.get(
    "/settings/notifications/by-username/{username}",
    response_model=NotificationPreferenceResponse,
    tags=["S · 設定"],
)
async def get_notification_pref(
    username: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not user.is_superuser and username != user.username:
        raise HTTPException(403, "Cannot read another user's notification preferences")
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
    username: str,
    payload: NotificationPreferenceUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not user.is_superuser and username != user.username:
        raise HTTPException(403, "Cannot update another user's notification preferences")
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


# ─── EmailConfig（每個 org 一份） ──────────────────────────────────────

async def _get_or_create_email_for_org(db: AsyncSession, org_id: Optional[str]) -> EmailConfig:
    """以 organization_id 為主鍵尋找；找不到就建一筆。"""
    stmt = select(EmailConfig).where(EmailConfig.organization_id == org_id)
    cfg = (await db.execute(stmt)).scalar_one_or_none()
    if cfg:
        return cfg
    # 用 org_id 作為主鍵（避免 collision；若 org_id 為 None 用 "default" 字串）
    cfg = EmailConfig(id=org_id or "default", organization_id=org_id)
    db.add(cfg)
    await db.flush()
    await db.refresh(cfg)
    return cfg


@router.get(
    "/settings/email",
    response_model=EmailConfigResponse,
    tags=["S · 設定"],
    dependencies=[Depends(require_casbin(P.SETTINGS_READ))],
)
async def get_email_config(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return _email_to_response(await _get_or_create_email_for_org(db, user.organization_id))


@router.put(
    "/settings/email",
    response_model=EmailConfigResponse,
    tags=["S · 設定"],
    dependencies=[Depends(require_casbin(P.SETTINGS_WRITE))],
)
async def update_email_config(
    payload: EmailConfigUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cfg = await _get_or_create_email_for_org(db, user.organization_id)
    data = payload.model_dump(exclude_unset=True)
    if "smtp_password" in data and not data["smtp_password"]:
        data.pop("smtp_password")
    for k, v in data.items():
        setattr(cfg, k, v)
    await db.flush()
    await db.refresh(cfg)
    return _email_to_response(cfg)


class _EmailTestRequest(BaseModel):
    """Body for POST /api/settings/email/test."""
    to: Optional[str] = None  # default = current user's email


class _EmailTestResponse(BaseModel):
    sent: bool
    to: str
    detail: Optional[str] = None


@router.post(
    "/settings/email/test",
    response_model=_EmailTestResponse,
    tags=["S · 設定"],
    dependencies=[Depends(require_casbin(P.SETTINGS_WRITE))],
)
async def send_test_email(
    payload: _EmailTestRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a small test email using the saved EmailConfig.

    Sync send (not via Celery) so the admin gets immediate pass/fail
    feedback without polling Celery state. Failures return 4xx with the
    SMTP error in `detail` so the admin can fix host/port/auth quickly.
    """
    target = (payload.to or user.email or "").strip()
    if not target:
        raise HTTPException(400, "尚未設定 email,請改用 ?to=... 指定收件者")

    from app.services.email_service import (
        EmailNotConfigured,
        EmailSendFailed,
        _send_with_config,
    )

    # Load EmailConfig asynchronously (AsyncSession cannot use sync .execute())
    org_id = user.organization_id
    cfg = (await db.execute(
        select(EmailConfig).where(EmailConfig.organization_id == org_id)
    )).scalar_one_or_none()
    if cfg is None:
        cfg = await db.get(EmailConfig, "default")
    if cfg is None or not cfg.enabled:
        raise HTTPException(400, f"EmailConfig 尚未啟用或不完整：org_id={org_id!r}")
    if not (cfg.smtp_host and cfg.from_address):
        raise HTTPException(400, "EmailConfig 缺少 smtp_host 或 from_address")

    subject = f"AutoTest SMTP 測試信 - {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    body_text = (
        "這是一封來自 AutoTest 的 SMTP 測試信。\n\n"
        f"觸發者:{user.username}\n"
        f"組織:{org_id or '(none)'}\n"
        "若您收到此信,代表 EmailConfig 設定正確,通知/邀請信會循同樣管道送達。"
    )
    body_html = (
        "<p>這是一封來自 <b>AutoTest</b> 的 SMTP 測試信。</p>"
        f"<p>觸發者:<code>{user.username}</code><br>"
        f"組織:<code>{org_id or '(none)'}</code></p>"
        "<p>若您收到此信,代表 EmailConfig 設定正確,通知/邀請信會循同樣管道送達。</p>"
    )
    try:
        _send_with_config(cfg, to=target, subject=subject, html_body=body_html, text_body=body_text)
    except EmailSendFailed as exc:
        raise HTTPException(status_code=502, detail=f"SMTP 發送失敗:{exc}")
    return _EmailTestResponse(sent=True, to=target)


