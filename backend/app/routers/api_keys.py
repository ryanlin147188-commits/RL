"""API Key 管理 — v1.1.10 加。

提供 superuser CRUD + gateway 內部用的 verify endpoint。

* ``POST   /api/auth/api-keys`` 新增(superuser only),回 plain key 一次
* ``GET    /api/auth/api-keys`` 列(superuser see 全;一般 user see 自己的)
* ``DELETE /api/auth/api-keys/{id}`` 撤銷(superuser 或 owner)
* ``POST   /api/auth/api-keys/verify`` 給 gateway 內部用:用 X-Gateway-* 信任
  header + ``key_hash`` 查 user,回 JWT mint metadata。**不對外暴露**;gateway
  以 ``GATEWAY_BACKEND_SHARED_SECRET`` HMAC 簽 request,backend AuthMiddleware
  短路 + 這個 endpoint 額外擋一道 sub=='gateway' 才能呼叫。
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.api_key import ApiKey
from app.models.user import User

router = APIRouter()


# ── Pydantic schemas ───────────────────────────────────────────
class ApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    scopes: Optional[list[str]] = None
    expires_at: Optional[datetime] = None


class ApiKeyResponse(BaseModel):
    id: str
    name: str
    key_prefix: str
    user_id: str
    scopes: Optional[list[str]] = None
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    revoked: bool
    created_at: datetime


class ApiKeyCreateResponse(ApiKeyResponse):
    # 明碼 key — 只在 POST 時回一次,user 自己存
    key: str


class ApiKeyVerifyResponse(BaseModel):
    """Gateway 內部用 — 收到 X-API-Key 後查這支,回 JWT mint 需要的 metadata。"""
    user_id: str
    username: str
    organization_id: Optional[str] = None
    is_superuser: bool
    scopes: Optional[list[str]] = None


# ── Helpers ────────────────────────────────────────────────────
_PLAIN_KEY_PREFIX = "ak_"


def _generate_plain_key() -> tuple[str, str, str]:
    """產一把新 key。回 (plain_key, key_hash, key_prefix)。

    plain key 格式:``ak_<32 hex chars>``,長度 35。SHA256 hash 存 DB。
    key_prefix 留前 12 字(含 ``ak_``)給 UI 顯示。
    """
    raw = secrets.token_hex(16)  # 32 hex chars
    plain = _PLAIN_KEY_PREFIX + raw
    key_hash = hashlib.sha256(plain.encode("utf-8")).hexdigest()
    key_prefix = plain[:12]
    return plain, key_hash, key_prefix


def _require_superuser(user: User) -> None:
    if not user.is_superuser:
        raise HTTPException(403, "需要 superuser 權限")


# ── CRUD endpoints ─────────────────────────────────────────────
@router.post(
    "/auth/api-keys",
    response_model=ApiKeyCreateResponse,
    tags=["U · 認證"],
    status_code=201,
)
async def create_api_key(
    payload: ApiKeyCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """新增 API key — 只 superuser 可建,且綁在自己 user_id 上(可改)。"""
    _require_superuser(user)
    plain, key_hash, key_prefix = _generate_plain_key()
    ak = ApiKey(
        id=str(uuid.uuid4()),
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=payload.name,
        user_id=user.id,
        organization_id=user.organization_id,
        scopes=payload.scopes,
        expires_at=payload.expires_at,
    )
    db.add(ak)
    await db.commit()
    await db.refresh(ak)
    return ApiKeyCreateResponse(
        id=ak.id, name=ak.name, key_prefix=ak.key_prefix, user_id=ak.user_id,
        scopes=ak.scopes, expires_at=ak.expires_at, last_used_at=ak.last_used_at,
        revoked=ak.revoked, created_at=ak.created_at, key=plain,
    )


@router.get(
    "/auth/api-keys",
    response_model=list[ApiKeyResponse],
    tags=["U · 認證"],
)
async def list_api_keys(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """列 — superuser see 全部,一般 user 只看自己 own 的。"""
    stmt = select(ApiKey).order_by(ApiKey.created_at.desc())
    if not user.is_superuser:
        stmt = stmt.where(ApiKey.user_id == user.id)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.delete(
    "/auth/api-keys/{key_id}",
    tags=["U · 認證"],
    status_code=204,
)
async def revoke_api_key(
    key_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ak = await db.get(ApiKey, key_id)
    if not ak:
        raise HTTPException(404, "key not found")
    if not user.is_superuser and ak.user_id != user.id:
        raise HTTPException(403, "no permission")
    ak.revoked = True
    await db.commit()
    return None


# ── Gateway 內部用:verify endpoint ─────────────────────────────
@router.post(
    "/auth/api-keys/verify",
    response_model=ApiKeyVerifyResponse,
    tags=["U · 認證"],
    include_in_schema=False,
)
async def verify_api_key(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Gateway 內部用:給 X-API-Key 明碼,回對應 user 資料給 gateway 再 mint JWT。

    Security:這支 endpoint **只接受帶 ``X-Gateway-Verified`` HMAC 的 request**
    (sub=gateway/internal),非 gateway 不能呼叫。AuthMiddleware 已經短路驗過
    HMAC,所以這裡只要看 ``request.state.user_payload['sub'] == 'gateway'`` 就放
    行;backend 自己呼叫(沒 X-Gateway-*)也行,但需 is_superuser=True。
    """
    payload = getattr(request.state, "user_payload", None) or {}
    is_gateway_call = payload.get("sub") == "gateway"
    if not is_gateway_call and not payload.get("is_superuser"):
        raise HTTPException(403, "only callable by gateway or superuser")

    body = await request.json()
    plain = (body or {}).get("api_key") or ""
    if not plain.startswith(_PLAIN_KEY_PREFIX):
        raise HTTPException(400, "invalid key format")
    key_hash = hashlib.sha256(plain.encode("utf-8")).hexdigest()

    ak = (await db.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))).scalar_one_or_none()
    if not ak:
        raise HTTPException(404, "key not found")
    if ak.revoked:
        raise HTTPException(401, "key revoked")
    if ak.expires_at and ak.expires_at < datetime.utcnow():
        raise HTTPException(401, "key expired")

    # 更新 last_used_at(失敗也不擋驗證流程)
    try:
        ak.last_used_at = datetime.utcnow()
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()

    owner = await db.get(User, ak.user_id)
    if not owner or not owner.is_active:
        raise HTTPException(401, "key owner inactive")

    return ApiKeyVerifyResponse(
        user_id=owner.id,
        username=owner.username,
        organization_id=owner.organization_id,
        is_superuser=owner.is_superuser,
        scopes=ak.scopes,
    )
