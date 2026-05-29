"""MCP server CRUD + test + refresh tools REST endpoints — Phase 2b。

權限:
* list — get_current_user(任何使用者可看自己 org 有哪些 MCP server)
* create / update / delete / test / refresh — ``SETTINGS_WRITE``(機敏設定,
  含 secret 寫入)

紅線:
* response 永遠不含 secret 明文;只回 ref_name 清單
* update 時 secret value 為 ``""`` 表示「刪除該 ref」;null 不存在於 patch 內
  表示「不動」
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.permissions import require_casbin
from app.auth.permissions_catalog import P
from app.database import get_db
from app.models.mcp_server import MCPServer
from app.models.org_membership import OrgMembership
from app.models.user import User
from app.services import mcp_server_service

log = logging.getLogger(__name__)

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────


class MCPServerResponse(BaseModel):
    id: str
    organization_id: str
    name: str
    transport: str
    url: Optional[str] = None
    command: Optional[str] = None
    args_json: Optional[list[str]] = None
    env_json: Optional[dict[str, str]] = None
    headers_json: Optional[dict[str, str]] = None
    enabled: bool
    requires_confirmation: bool
    casbin_permission: Optional[str] = None
    last_health: str
    last_error: Optional[str] = None
    last_checked_at: Any = None
    # 永不回明文;前端用此判斷哪些 ref 已存
    secret_ref_names: list[str] = []
    tools_count: int = 0
    created_at: Any
    updated_at: Any


class MCPServerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    transport: str = "http"
    url: Optional[str] = None
    command: Optional[str] = None
    args_json: Optional[list[str]] = None
    env_json: Optional[dict[str, str]] = None
    headers_json: Optional[dict[str, str]] = None
    enabled: bool = True
    requires_confirmation: bool = True
    casbin_permission: Optional[str] = "mcp.use"
    # ``{ref_name: plain_value}``;只在 create / update 時 client 帶,response 不回
    secrets: Optional[dict[str, str]] = None


class MCPServerUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=64)
    transport: Optional[str] = None
    url: Optional[str] = None
    command: Optional[str] = None
    args_json: Optional[list[str]] = None
    env_json: Optional[dict[str, str]] = None
    headers_json: Optional[dict[str, str]] = None
    enabled: Optional[bool] = None
    requires_confirmation: Optional[bool] = None
    casbin_permission: Optional[str] = None
    # secrets_update: ``{ref_name: plain_value | ""}`` — ""=刪除
    secrets_update: Optional[dict[str, str]] = None


class MCPServerTestResponse(BaseModel):
    ok: bool
    tools_count: int
    error: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────


async def _check_org_access(db: AsyncSession, user: User, org_id: str) -> None:
    """非 superuser 只能存取自己有 active OrgMembership 的 org 的 MCP servers。

    舊版只比 ``user.organization_id``,但這個欄位是「使用者主要 / 預設 org」
    的快取;當使用者切到他被邀請進去的其他 org,frontend 用 active org id
    呼叫這個 endpoint 會被誤拒。改用 OrgMembership 才是真實成員關係。
    """
    if user.is_superuser:
        return
    if user.organization_id == org_id:
        return  # fast path:default org match 直接放行
    stmt = (
        select(OrgMembership)
        .where(OrgMembership.username == user.username)
        .where(OrgMembership.organization_id == org_id)
        .where(OrgMembership.status == "active")
    )
    if (await db.execute(stmt)).scalar_one_or_none() is None:
        raise HTTPException(403, "無權存取此組織的 MCP servers")


async def _to_response(
    db: AsyncSession, server: MCPServer
) -> dict[str, Any]:
    ref_names = await mcp_server_service.list_secret_refs(db, server_id=server.id)
    cached = await mcp_server_service.list_cached_tools(db, server_id=server.id)
    return {
        "id": server.id,
        "organization_id": server.organization_id,
        "name": server.name,
        "transport": server.transport,
        "url": server.url,
        "command": server.command,
        "args_json": server.args_json,
        "env_json": server.env_json,
        "headers_json": server.headers_json,
        "enabled": server.enabled,
        "requires_confirmation": server.requires_confirmation,
        "casbin_permission": server.casbin_permission,
        "last_health": server.last_health,
        "last_error": server.last_error,
        "last_checked_at": server.last_checked_at,
        "secret_ref_names": sorted(ref_names),
        "tools_count": len(cached),
        "created_at": server.created_at,
        "updated_at": server.updated_at,
    }


def _map_error(exc: mcp_server_service.MCPServerError) -> HTTPException:
    if isinstance(exc, mcp_server_service.MCPServerNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, mcp_server_service.MCPServerNameConflict):
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))


# ── Endpoints ────────────────────────────────────────────────────────


@router.get(
    "/v1/orgs/{org_id}/mcp-servers",
    response_model=list[MCPServerResponse],
    tags=["AE · Agent"],
)
async def list_mcp_servers(
    org_id: str = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _check_org_access(db, user, org_id)
    rows = await mcp_server_service.list_servers(db, organization_id=org_id)
    return [await _to_response(db, r) for r in rows]


@router.post(
    "/v1/orgs/{org_id}/mcp-servers",
    response_model=MCPServerResponse,
    tags=["AE · Agent"],
    dependencies=[Depends(require_casbin(P.SETTINGS_WRITE))],
)
async def create_mcp_server(
    payload: MCPServerCreate,
    org_id: str = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _check_org_access(db, user, org_id)
    try:
        server = await mcp_server_service.create_server(
            db,
            organization_id=org_id,
            created_by=user.id,
            name=payload.name,
            transport=payload.transport,
            url=payload.url,
            command=payload.command,
            args_json=payload.args_json,
            env_json=payload.env_json,
            headers_json=payload.headers_json,
            secrets=payload.secrets,
            enabled=payload.enabled,
            requires_confirmation=payload.requires_confirmation,
            casbin_permission=payload.casbin_permission,
        )
    except mcp_server_service.MCPServerError as exc:
        raise _map_error(exc) from exc
    return await _to_response(db, server)


@router.put(
    "/v1/orgs/{org_id}/mcp-servers/{server_id}",
    response_model=MCPServerResponse,
    tags=["AE · Agent"],
    dependencies=[Depends(require_casbin(P.SETTINGS_WRITE))],
)
async def update_mcp_server(
    payload: MCPServerUpdate,
    org_id: str = Path(...),
    server_id: str = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _check_org_access(db, user, org_id)
    body = payload.model_dump(exclude_unset=True)
    secrets_update_raw = body.pop("secrets_update", None)
    # 把 "" 翻譯成 None(刪除);保留非空值
    secrets_update: Optional[dict[str, Optional[str]]] = None
    if secrets_update_raw is not None:
        secrets_update = {
            k: (v if v != "" else None) for k, v in secrets_update_raw.items()
        }
    try:
        server = await mcp_server_service.update_server(
            db,
            server_id=server_id,
            organization_id=org_id,
            payload=body,
            secrets_update=secrets_update,
        )
    except mcp_server_service.MCPServerError as exc:
        raise _map_error(exc) from exc
    return await _to_response(db, server)


@router.delete(
    "/v1/orgs/{org_id}/mcp-servers/{server_id}",
    status_code=204,
    tags=["AE · Agent"],
    dependencies=[Depends(require_casbin(P.SETTINGS_WRITE))],
)
async def delete_mcp_server(
    org_id: str = Path(...),
    server_id: str = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _check_org_access(db, user, org_id)
    try:
        await mcp_server_service.delete_server(
            db, server_id=server_id, organization_id=org_id
        )
    except mcp_server_service.MCPServerError as exc:
        raise _map_error(exc) from exc
    return None


@router.post(
    "/v1/orgs/{org_id}/mcp-servers/{server_id}/test",
    response_model=MCPServerTestResponse,
    tags=["AE · Agent"],
    dependencies=[Depends(require_casbin(P.SETTINGS_WRITE))],
)
async def test_mcp_server_connection(
    org_id: str = Path(...),
    server_id: str = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _check_org_access(db, user, org_id)
    try:
        server = await mcp_server_service.get_server(
            db, server_id=server_id, organization_id=org_id
        )
    except mcp_server_service.MCPServerError as exc:
        raise _map_error(exc) from exc
    return await mcp_server_service.test_connection(db, server=server)


@router.post(
    "/v1/orgs/{org_id}/mcp-servers/{server_id}/refresh-tools",
    response_model=MCPServerTestResponse,
    tags=["AE · Agent"],
    dependencies=[Depends(require_casbin(P.SETTINGS_WRITE))],
)
async def refresh_mcp_tools(
    org_id: str = Path(...),
    server_id: str = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _check_org_access(db, user, org_id)
    try:
        server = await mcp_server_service.get_server(
            db, server_id=server_id, organization_id=org_id
        )
    except mcp_server_service.MCPServerError as exc:
        raise _map_error(exc) from exc
    # 等同 test 的行為 — 內部就是同一條 path
    return await mcp_server_service.test_connection(db, server=server)
