"""MCP server CRUD + secret 管理 + test_connection + refresh_tools_cache。

設計重點:
* secret 永遠不從 GET response 回傳明文;只回 ``has_secret: bool`` + ref_name
* env_json / headers_json 存的是 ref_name(對應 mcp_server_secrets.ref_name)
* test_connection 真實 spawn 一次連線跑 list_tools — 同時更新 last_health
* refresh_tools_cache 寫入 mcp_tools_cache,供 compose_tools_for_session 使用
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.client import MCPClient, MCPConnectionError, MCPError
from app.mcp.connection_pool import POOL
from app.models.mcp_server import (
    ALLOWED_MCP_TRANSPORTS,
    MCP_HEALTH_CONNECTED,
    MCP_HEALTH_ERROR,
    MCP_TRANSPORT_HTTP,
    MCPServer,
    MCPServerSecret,
    MCPToolCache,
)

log = logging.getLogger(__name__)


class MCPServerError(Exception):
    """router 轉 HTTPException 用。"""


class MCPServerNotFound(MCPServerError):
    pass


class MCPServerNameConflict(MCPServerError):
    pass


# ── CRUD ──────────────────────────────────────────────────────────────


async def list_servers(
    db: AsyncSession,
    *,
    organization_id: str,
    enabled_only: bool = False,
) -> list[MCPServer]:
    stmt = select(MCPServer).where(MCPServer.organization_id == organization_id)
    if enabled_only:
        stmt = stmt.where(MCPServer.enabled == True)  # noqa: E712
    stmt = stmt.order_by(MCPServer.name)
    return list((await db.execute(stmt)).scalars().all())


async def get_server(
    db: AsyncSession, *, server_id: str, organization_id: str
) -> MCPServer:
    stmt = select(MCPServer).where(
        MCPServer.id == server_id,
        MCPServer.organization_id == organization_id,
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise MCPServerNotFound(f"MCP server {server_id} 不存在")
    return row


async def get_by_name(
    db: AsyncSession, *, name: str, organization_id: str
) -> Optional[MCPServer]:
    stmt = select(MCPServer).where(
        MCPServer.organization_id == organization_id,
        MCPServer.name == name,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _validate_transport(transport: str) -> None:
    if transport not in ALLOWED_MCP_TRANSPORTS:
        raise MCPServerError(
            f"transport 必須是 {ALLOWED_MCP_TRANSPORTS},收到:{transport!r}"
        )


def _validate_http_url(url: Optional[str]) -> None:
    if not url:
        raise MCPServerError("transport=http 需要 url 欄位")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise MCPServerError("url 必須以 http:// 或 https:// 開頭")


# Phase 3 紅線:容器內只允許白名單命令,避免任意 RCE 透過 MCP server config
# 注入(admin 雖然要 SETTINGS_WRITE,但二次防護仍必要)。要加新 runtime 走
# code review 加進此列。
ALLOWED_STDIO_COMMANDS = frozenset({"npx", "uvx", "node", "python", "python3"})


def _validate_stdio_command(command: Optional[str], args: Optional[list[str]]) -> None:
    if not command:
        raise MCPServerError("transport=stdio 需要 command 欄位")
    # 容忍絕對路徑(/usr/bin/npx 等);基底名要在白名單
    base = command.rsplit("/", 1)[-1]
    if base not in ALLOWED_STDIO_COMMANDS:
        raise MCPServerError(
            f"stdio command 不在白名單(允許:{sorted(ALLOWED_STDIO_COMMANDS)}),收到:{command!r}"
        )
    if args is not None and not isinstance(args, list):
        raise MCPServerError("args 必須是 list[str]")
    if args:
        for a in args:
            if not isinstance(a, str):
                raise MCPServerError("args 每個元素都必須是字串")


async def create_server(
    db: AsyncSession,
    *,
    organization_id: str,
    created_by: Optional[str],
    name: str,
    transport: str,
    url: Optional[str] = None,
    command: Optional[str] = None,
    args_json: Optional[list[str]] = None,
    env_json: Optional[dict[str, str]] = None,
    headers_json: Optional[dict[str, str]] = None,
    secrets: Optional[dict[str, str]] = None,
    enabled: bool = True,
    requires_confirmation: bool = True,
    casbin_permission: Optional[str] = "mcp.use",
) -> MCPServer:
    """建立 MCP server + 寫對應 secrets。

    ``secrets``: ``{ref_name: plain_value}``;ref_name 必須對齊 env_json /
    headers_json 內被引用的 key。
    """
    name = (name or "").strip()
    if not name:
        raise MCPServerError("name 不可為空")
    if len(name) > 64:
        raise MCPServerError("name 長度超過 64")
    # double-underscore 用於 mcp__server__tool 的分隔;name 自己就含 __ 會破壞 parse
    if "__" in name:
        raise MCPServerError("name 不可包含連續底線 (__)")

    _validate_transport(transport)
    if transport == MCP_TRANSPORT_HTTP:
        _validate_http_url(url)
    else:
        _validate_stdio_command(command, args_json)
    dup = await get_by_name(db, name=name, organization_id=organization_id)
    if dup is not None:
        raise MCPServerNameConflict(f"MCP server name '{name}' 已存在")

    server = MCPServer(
        id=str(uuid.uuid4()),
        organization_id=organization_id,
        created_by=created_by,
        name=name,
        transport=transport,
        command=command,
        args_json=args_json,
        url=url,
        env_json=env_json,
        headers_json=headers_json,
        enabled=enabled,
        requires_confirmation=requires_confirmation,
        casbin_permission=casbin_permission,
    )
    db.add(server)
    await db.flush()

    if secrets:
        for ref_name, value in secrets.items():
            db.add(MCPServerSecret(
                id=str(uuid.uuid4()),
                server_id=server.id,
                ref_name=ref_name,
                value=value,
            ))
        await db.flush()

    await db.refresh(server)
    return server


async def update_server(
    db: AsyncSession,
    *,
    server_id: str,
    organization_id: str,
    payload: dict[str, Any],
    secrets_update: Optional[dict[str, Optional[str]]] = None,
) -> MCPServer:
    """更新 MCP server。

    ``secrets_update``: ``{ref_name: plain_value | None}`` — None 表示刪除
    該 ref;明文非空表示新增 / 覆寫。已存在但不在這 dict 內的 secret 不動。
    """
    server = await get_server(
        db, server_id=server_id, organization_id=organization_id
    )

    if "name" in payload:
        new_name = (payload["name"] or "").strip()
        if not new_name:
            raise MCPServerError("name 不可為空")
        if "__" in new_name:
            raise MCPServerError("name 不可包含連續底線 (__)")
        if new_name != server.name:
            dup = await get_by_name(
                db, name=new_name, organization_id=organization_id
            )
            if dup is not None:
                raise MCPServerNameConflict(f"MCP server name '{new_name}' 已存在")
            server.name = new_name
    if "transport" in payload:
        _validate_transport(payload["transport"])
        server.transport = payload["transport"]
    if "url" in payload:
        if server.transport == MCP_TRANSPORT_HTTP:
            _validate_http_url(payload["url"])
        server.url = payload["url"]
    # stdio 欄位:在 service 層做一次 sanity check;若 transport 是 stdio 且這次
    # 改了 command/args,跑白名單驗證(transport=http 不檢查,維持彈性)
    new_command = payload.get("command", server.command)
    new_args = payload.get("args_json", server.args_json)
    if server.transport == "stdio" and ("command" in payload or "args_json" in payload):
        _validate_stdio_command(new_command, new_args)
    for field in ("command", "args_json", "env_json", "headers_json", "casbin_permission"):
        if field in payload:
            setattr(server, field, payload[field])
    if "enabled" in payload:
        server.enabled = bool(payload["enabled"])
    if "requires_confirmation" in payload:
        server.requires_confirmation = bool(payload["requires_confirmation"])

    if secrets_update:
        for ref_name, value in secrets_update.items():
            stmt = select(MCPServerSecret).where(
                MCPServerSecret.server_id == server.id,
                MCPServerSecret.ref_name == ref_name,
            )
            existing = (await db.execute(stmt)).scalar_one_or_none()
            if value is None:
                if existing is not None:
                    await db.delete(existing)
            else:
                if existing is None:
                    db.add(MCPServerSecret(
                        id=str(uuid.uuid4()),
                        server_id=server.id,
                        ref_name=ref_name,
                        value=value,
                    ))
                else:
                    existing.value = value

    # config 改動 → 連線可能要重連 + tools cache 可能 stale
    await POOL.invalidate(server.id)
    await _clear_tools_cache(db, server.id)

    await db.flush()
    await db.refresh(server)
    return server


async def delete_server(
    db: AsyncSession, *, server_id: str, organization_id: str
) -> None:
    server = await get_server(
        db, server_id=server_id, organization_id=organization_id
    )
    await POOL.invalidate(server.id)
    await db.delete(server)
    await db.flush()


# ── Secrets resolution ────────────────────────────────────────────────


async def get_secrets_for_server(
    db: AsyncSession, *, server_id: str
) -> dict[str, str]:
    """回 ``{ref_name: plain_value}``(EncryptedString 已自動解密)。"""
    stmt = select(MCPServerSecret).where(MCPServerSecret.server_id == server_id)
    rows = (await db.execute(stmt)).scalars().all()
    return {r.ref_name: (r.value or "") for r in rows}


async def list_secret_refs(
    db: AsyncSession, *, server_id: str
) -> list[str]:
    """只回 ref name(用於 list response — 永遠不回明文)。"""
    stmt = select(MCPServerSecret.ref_name).where(
        MCPServerSecret.server_id == server_id
    )
    return [row[0] for row in (await db.execute(stmt)).all()]


async def resolve_headers(
    db: AsyncSession, *, server: MCPServer
) -> dict[str, str]:
    """把 headers_json 內的 secret_ref 替換成明文。"""
    if not server.headers_json:
        return {}
    secrets = await get_secrets_for_server(db, server_id=server.id)
    out: dict[str, str] = {}
    for header_name, ref in (server.headers_json or {}).items():
        # value 可能是 ref_name(走 secrets dict 找) 或直接是明文(legacy / 不機敏)
        if isinstance(ref, str) and ref in secrets:
            out[header_name] = secrets[ref]
        else:
            out[header_name] = ref if isinstance(ref, str) else str(ref)
    return out


async def resolve_env(
    db: AsyncSession, *, server: MCPServer
) -> dict[str, str]:
    """把 env_json 內的 secret_ref 替換成明文(stdio transport 用)。

    與 resolve_headers 同樣邏輯;分開以利後續 stdio 專屬調整。
    """
    if not server.env_json:
        return {}
    secrets = await get_secrets_for_server(db, server_id=server.id)
    out: dict[str, str] = {}
    for env_name, ref in (server.env_json or {}).items():
        if isinstance(ref, str) and ref in secrets:
            out[env_name] = secrets[ref]
        else:
            out[env_name] = ref if isinstance(ref, str) else str(ref)
    return out


# ── Tools cache ───────────────────────────────────────────────────────


async def _clear_tools_cache(db: AsyncSession, server_id: str) -> None:
    await db.execute(
        delete(MCPToolCache).where(MCPToolCache.server_id == server_id)
    )


async def list_cached_tools(
    db: AsyncSession, *, server_id: str
) -> list[MCPToolCache]:
    stmt = select(MCPToolCache).where(MCPToolCache.server_id == server_id)
    return list((await db.execute(stmt)).scalars().all())


async def refresh_tools_cache(
    db: AsyncSession, *, server: MCPServer
) -> int:
    """連到 server 跑 list_tools → 整批寫入 mcp_tools_cache。回 tool 數量。

    成功時順便把 ``last_health = 'connected'``;失敗 raise MCPError(由 caller
    決定要不要 catch 寫 health=error)。
    """
    headers = await resolve_headers(db, server=server)
    env = await resolve_env(db, server=server) if server.transport == "stdio" else {}

    async def _do() -> list[dict[str, Any]]:
        # 用獨立 client(不走 POOL)— refresh 本來就少做,壽命短不必占 pool slot
        client = await MCPClient.open(
            server_id=server.id,
            transport=server.transport,
            url=server.url,
            headers=headers,
            command=server.command,
            args=server.args_json,
            env=env,
        )
        try:
            return await client.list_tools()
        finally:
            await client.close()

    tools = await asyncio.wait_for(_do(), timeout=30.0)
    await _clear_tools_cache(db, server.id)
    for t in tools:
        db.add(MCPToolCache(
            id=str(uuid.uuid4()),
            server_id=server.id,
            tool_name=t.get("name") or "",
            description=t.get("description") or "",
            input_schema=t.get("input_schema") or {},
            fetched_at=datetime.utcnow(),
        ))
    server.last_health = MCP_HEALTH_CONNECTED
    server.last_error = None
    server.last_checked_at = datetime.utcnow()
    await db.flush()
    return len(tools)


async def test_connection(
    db: AsyncSession, *, server: MCPServer
) -> dict[str, Any]:
    """連 + list_tools + 寫 health。永遠不會 raise — 失敗時把錯誤包進回傳值。"""
    try:
        count = await refresh_tools_cache(db, server=server)
        return {"ok": True, "tools_count": count, "error": None}
    except (MCPError, asyncio.TimeoutError) as exc:
        server.last_health = MCP_HEALTH_ERROR
        server.last_error = f"{type(exc).__name__}: {exc}"
        server.last_checked_at = datetime.utcnow()
        await db.flush()
        return {
            "ok": False,
            "tools_count": 0,
            "error": server.last_error,
        }
    except Exception as exc:  # noqa: BLE001 — 任何意外都不該讓 test endpoint 500
        log.exception("MCP test_connection unexpected failure (server_id=%s)", server.id)
        server.last_health = MCP_HEALTH_ERROR
        server.last_error = f"{type(exc).__name__}: {exc}"
        server.last_checked_at = datetime.utcnow()
        await db.flush()
        return {"ok": False, "tools_count": 0, "error": server.last_error}
