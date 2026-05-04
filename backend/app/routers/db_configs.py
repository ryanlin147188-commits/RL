"""DB Connection Config REST endpoints — DB 持久化(取代前端 localStorage)。

API 對外用 `password` 欄位(明文);DB 落地用 `password_encrypted`(Fernet 密文)。
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.permissions import require_permission
from app.auth.permissions_catalog import P
from app.auth.project_membership import ensure_project_member
from app.auth.scope import ensure_project_writable
from app.database import get_db
from app.models.db_config import DbConfig
from app.models.user import User
from app.schemas.db_config import (
    DbConfigCreate,
    DbConfigResponse,
    DbConfigUpdate,
)

router = APIRouter()

_VALID_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VALID_TYPES = {"mysql", "postgresql", "mssql", "oracle", "mongodb", "redis", "sqlite"}


def _to_response(d: DbConfig) -> dict:
    """ORM → dict; never return the decrypted password."""
    return {
        "id": d.id,
        "project_id": d.project_id,
        "organization_id": d.organization_id,
        "name": d.name,
        "db_type": d.db_type,
        "host": d.host,
        "port": d.port,
        "database": d.database,
        "username": d.username,
        "password": None,
        "has_password": bool(d.password_encrypted),
        "extra_options": d.extra_options,
        "custom_dsn": d.custom_dsn,
        "description": d.description,
        "enabled": d.enabled,
        "created_at": d.created_at,
        "updated_at": d.updated_at,
    }


def _validate(payload_dict: dict) -> None:
    if "name" in payload_dict and payload_dict["name"] is not None:
        if not _VALID_NAME_RE.match(payload_dict["name"]):
            raise HTTPException(
                400,
                f"name 不合法:{payload_dict['name']}(限英數+底線、不可數字開頭)",
            )
    if "db_type" in payload_dict and payload_dict["db_type"] is not None:
        if payload_dict["db_type"].lower() not in _VALID_TYPES:
            raise HTTPException(400, f"db_type 必須是 {sorted(_VALID_TYPES)} 之一")


@router.get(
    "/db-configs",
    response_model=list[DbConfigResponse],
    tags=["AA · DB 連線"],
    dependencies=[
        Depends(require_permission(P.SETTINGS_READ)),
        Depends(ensure_project_member),
    ],
)
async def list_db_configs(
    project_id: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(DbConfig).order_by(desc(DbConfig.created_at))
    if user.organization_id:
        stmt = stmt.where(DbConfig.organization_id == user.organization_id)
    if project_id:
        stmt = stmt.where(DbConfig.project_id == project_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [_to_response(r) for r in rows]


@router.post(
    "/db-configs",
    response_model=DbConfigResponse,
    status_code=201,
    tags=["AA · DB 連線"],
    dependencies=[Depends(require_permission(P.SETTINGS_WRITE))],
)
async def create_db_config(
    payload: DbConfigCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _validate(payload.model_dump())
    if payload.project_id:
        await ensure_project_writable(db, payload.project_id, user)
    d = DbConfig(
        organization_id=user.organization_id,
        project_id=payload.project_id,
        name=payload.name,
        db_type=payload.db_type.lower(),
        host=payload.host,
        port=payload.port,
        database=payload.database,
        username=payload.username,
        password_encrypted=payload.password,
        extra_options=payload.extra_options,
        custom_dsn=payload.custom_dsn,
        description=payload.description,
        enabled=payload.enabled,
    )
    db.add(d)
    await db.flush()
    await db.refresh(d)
    return _to_response(d)


@router.get(
    "/db-configs/{cfg_id}",
    response_model=DbConfigResponse,
    tags=["AA · DB 連線"],
    dependencies=[Depends(require_permission(P.SETTINGS_READ))],
)
async def get_db_config(
    cfg_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    d = await db.get(DbConfig, cfg_id)
    if not d or (user.organization_id and d.organization_id != user.organization_id):
        raise HTTPException(404, "DB config not found")
    return _to_response(d)


@router.put(
    "/db-configs/{cfg_id}",
    response_model=DbConfigResponse,
    tags=["AA · DB 連線"],
    dependencies=[Depends(require_permission(P.SETTINGS_WRITE))],
)
async def update_db_config(
    cfg_id: str,
    payload: DbConfigUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    d = await db.get(DbConfig, cfg_id)
    if not d or (user.organization_id and d.organization_id != user.organization_id):
        raise HTTPException(404, "DB config not found")
    data = payload.model_dump(exclude_unset=True)
    _validate(data)
    if "db_type" in data and data["db_type"]:
        data["db_type"] = data["db_type"].lower()
    if "password" in data and data["password"]:
        d.password_encrypted = data.pop("password")
    elif "password" in data:
        data.pop("password")
    for key, val in data.items():
        setattr(d, key, val)
    await db.flush()
    await db.refresh(d)
    return _to_response(d)


@router.delete(
    "/db-configs/{cfg_id}",
    status_code=204,
    tags=["AA · DB 連線"],
    dependencies=[Depends(require_permission(P.SETTINGS_WRITE))],
)
async def delete_db_config(
    cfg_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    d = await db.get(DbConfig, cfg_id)
    if not d or (user.organization_id and d.organization_id != user.organization_id):
        raise HTTPException(404, "DB config not found")
    await db.delete(d)
    await db.flush()
