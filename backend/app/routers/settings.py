"""Settings 相關 REST endpoints（Role / Notification / Email / AI Token）。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import asc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.ai_token_config import AiProvider, AiTokenConfig
from app.models.email_config import EmailConfig
from app.models.notification_preference import NotificationPreference
from app.models.role import Role
from app.models.user import User
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


@router.get("/settings/permissions/catalogue", tags=["S · 設定"])
async def list_permission_catalogue():
    """前端建構角色 checkbox 用：所有可指派的權限 key 與顯示名稱。"""
    return {"items": _PERMISSION_CATALOGUE}


@router.get("/settings/notifications/catalogue", tags=["S · 設定"])
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


@router.get("/settings/roles", response_model=list[RoleResponse], tags=["S · 設定"])
async def list_roles(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    stmt = select(Role).order_by(asc(Role.name))
    stmt = _role_visibility_filter(stmt, user)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.post(
    "/settings/roles", response_model=RoleResponse, status_code=201, tags=["S · 設定"]
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
    "/settings/roles/{role_id}", response_model=RoleResponse, tags=["S · 設定"]
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
    return r


@router.delete("/settings/roles/{role_id}", status_code=204, tags=["S · 設定"])
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


@router.get("/settings/email", response_model=EmailConfigResponse, tags=["S · 設定"])
async def get_email_config(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _get_or_create_email_for_org(db, user.organization_id)


@router.put("/settings/email", response_model=EmailConfigResponse, tags=["S · 設定"])
async def update_email_config(
    payload: EmailConfigUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cfg = await _get_or_create_email_for_org(db, user.organization_id)
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(cfg, k, v)
    await db.flush()
    await db.refresh(cfg)
    return cfg


# ─── AiTokenConfig ────────────────────────────────────────────────────

def _ai_org_filter(stmt, user: User):
    if user.is_superuser:
        return stmt
    return stmt.where(AiTokenConfig.organization_id == user.organization_id)


def _check_ai_token_or_404(t: Optional[AiTokenConfig], user: User) -> AiTokenConfig:
    if not t:
        raise HTTPException(404, "AI token not found")
    if not user.is_superuser and t.organization_id != user.organization_id:
        raise HTTPException(404, "AI token not found")
    return t


@router.get(
    "/settings/ai-tokens", response_model=list[AiTokenConfigResponse], tags=["S · 設定"]
)
async def list_ai_tokens(
    provider: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AiTokenConfig).order_by(asc(AiTokenConfig.provider), asc(AiTokenConfig.name))
    stmt = _ai_org_filter(stmt, user)
    if provider:
        # provider 改自由字串(2026-04 重設計);舊 enum 行為不再
        stmt = stmt.where(AiTokenConfig.provider == provider)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.post(
    "/settings/ai-tokens",
    response_model=AiTokenConfigResponse,
    status_code=201,
    tags=["S · 設定"],
)
async def create_ai_token(
    payload: AiTokenConfigCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # provider 改自由字串(常見:OpenAI / Anthropic / DeepSeek / Groq / 自架...)
    provider_str = (payload.provider or "OpenAI").strip()
    if not provider_str:
        raise HTTPException(400, "provider 不能為空")
    token = AiTokenConfig(
        name=payload.name,
        organization_id=user.organization_id,
        provider=provider_str,
        api_key=payload.api_key,
        base_url=payload.base_url,
        model=payload.model,
        reasoning_effort=getattr(payload, "reasoning_effort", None),
        enabled=payload.enabled,
        is_default=payload.is_default,
        description=payload.description,
    )
    db.add(token)
    await db.flush()
    if token.is_default:
        # 同 org + 同 provider 內只能有一個 default
        await db.execute(
            update(AiTokenConfig)
            .where(
                AiTokenConfig.organization_id == user.organization_id,
                AiTokenConfig.provider == provider_str,
                AiTokenConfig.id != token.id,
            )
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
    token_id: str,
    payload: AiTokenConfigUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    t = _check_ai_token_or_404(await db.get(AiTokenConfig, token_id), user)
    data = payload.model_dump(exclude_unset=True)
    if "provider" in data and data["provider"] is not None:
        data["provider"] = (data["provider"] or "").strip() or t.provider
    for k, v in data.items():
        setattr(t, k, v)
    await db.flush()
    if t.is_default:
        await db.execute(
            update(AiTokenConfig)
            .where(
                AiTokenConfig.organization_id == t.organization_id,
                AiTokenConfig.provider == t.provider,
                AiTokenConfig.id != t.id,
            )
            .values(is_default=False)
        )
        await db.flush()
    await db.refresh(t)
    return t


class FetchModelsRequest(BaseModel):
    provider: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None  # 進階自訂端點(覆蓋 ai_provider_map 預設)


def _detect_reasoning_support(model_id: str) -> bool:
    """判斷某個模型是否支援 OpenAI 風格的 `reasoning_effort` 參數。

    依模型名稱 pattern 判定(provider 不會回這個欄位):
      - OpenAI o1 / o3 / o4 系列(o1-mini / o1-preview / o3 / o3-mini / o4-...)
      - 未來的 GPT-5 推理變體
      - DeepSeek R1 / 任何含 `reasoner` 字樣
    """
    if not model_id:
        return False
    m = model_id.lower()
    if m.startswith(("o1", "o3", "o4", "gpt-5")):
        return True
    if "reasoner" in m or "r1" in m:
        return True
    return False


@router.post(
    "/settings/ai-tokens/fetch-models",
    tags=["S · 設定"],
)
async def fetch_models(
    payload: FetchModelsRequest,
    user: User = Depends(get_current_user),
):
    """用使用者填的 provider + api_key 去打 provider 的 /models 端點,回傳模型清單。
    沒儲存 token 也可呼叫(讓使用者先試 key 再決定要不要存)。
    回應每個 model 帶 `supports_reasoning_effort` 標記讓前端決定是否讓使用者選思考程度。"""
    import httpx
    from app.services.ai_provider_map import resolve

    # 早擋:沒填 API key 就不用打了(本地 Ollama / LM Studio 例外)
    provider_lower = (payload.provider or "").strip().lower()
    if not payload.api_key and provider_lower not in {"ollama", "lmstudio", "lm studio"}:
        raise HTTPException(
            400,
            "請先填 API Key 才能拉模型清單(本地 Ollama / LM Studio 可不填)",
        )

    spec = resolve(payload.provider, base_url_override=payload.base_url)
    headers = {"Accept": "application/json"}
    if payload.api_key:
        headers[spec.auth_header] = (spec.auth_prefix or "") + payload.api_key
    if spec.extra_headers:
        headers.update(spec.extra_headers)
    url = spec.base_url.rstrip("/") + spec.models_path

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, headers=headers)
        if r.status_code == 401 or r.status_code == 403:
            raise HTTPException(401, f"{payload.provider} API key 驗證失敗(provider 回 {r.status_code})")
        if not r.is_success:
            raise HTTPException(502, f"{payload.provider} 回 {r.status_code}: {r.text[:300]}")
        data = r.json()
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(504, f"連線 {spec.base_url} 逾時")
    except Exception as e:
        raise HTTPException(502, f"連線 {payload.provider} 失敗:{type(e).__name__}: {e}")

    # 回應格式整理(OpenAI / Anthropic 兩種 schema 略有差異)
    items = data.get("data") or data.get("models") or []
    out = []
    for it in items:
        if isinstance(it, str):
            mid = it
            entry = {"id": mid, "name": mid}
        elif isinstance(it, dict):
            mid = it.get("id") or it.get("name") or it.get("model")
            if not mid:
                continue
            entry = {
                "id": mid,
                "name": it.get("display_name") or it.get("name") or mid,
                "context_length": it.get("context_length") or it.get("context_window"),
                "owned_by": it.get("owned_by"),
            }
        else:
            continue
        entry["supports_reasoning_effort"] = _detect_reasoning_support(entry["id"])
        out.append(entry)
    out.sort(key=lambda x: x["id"])
    return {"provider": payload.provider, "base_url": spec.base_url, "models": out}


@router.delete(
    "/settings/ai-tokens/{token_id}", status_code=204, tags=["S · 設定"]
)
async def delete_ai_token(
    token_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    t = _check_ai_token_or_404(await db.get(AiTokenConfig, token_id), user)
    await db.delete(t)
    await db.flush()
