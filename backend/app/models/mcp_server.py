"""MCP server ORM models — Phase 2b。

三個表:
* ``mcp_servers`` — per-org MCP server 設定(transport / url / 啟用旗標)
* ``mcp_server_secrets`` — 對應的 secret 值(Fernet 加密儲存)
* ``mcp_tools_cache`` — MCP server list_tools 結果快取,避免 chat 每輪 round-trip

設計重點:
* per-org `(organization_id, name)` 唯一。MCP tool 名稱對 LLM 是
  ``mcp__<server_name>__<tool_name>``,所以 server name 撞名也會撞 tool 名。
* transport 限 ``stdio`` / ``http``;Phase 2 只實作 http,Phase 3 加 stdio。
* env_json / headers_json 內存的是 **secret ref 名**(對應 mcp_server_secrets.ref_name),
  真實 token 永遠不會出現在 mcp_servers 表內,符合「audit log 不寫敏感資料」紅線。
* requires_confirmation 預設 True — MCP server 是外部不可信來源,任何 tool 都
  視為 destructive 直到 admin 確認該 server 安全為止。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.crypto import EncryptedString

from .base import Base


# transport 合法值;CHECK constraint 與 service 層共用
MCP_TRANSPORT_STDIO = "stdio"
MCP_TRANSPORT_HTTP = "http"
ALLOWED_MCP_TRANSPORTS = (MCP_TRANSPORT_STDIO, MCP_TRANSPORT_HTTP)

# health 合法值
MCP_HEALTH_UNKNOWN = "unknown"
MCP_HEALTH_CONNECTED = "connected"
MCP_HEALTH_DISCONNECTED = "disconnected"
MCP_HEALTH_ERROR = "error"


class MCPServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    transport: Mapped[str] = mapped_column(String(16), nullable=False)
    # stdio: ``npx`` / ``uvx`` / etc。Phase 2 不用;欄位先預留給 Phase 3。
    command: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # stdio 啟動參數
    args_json: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    # http: streamable HTTP endpoint URL
    url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # stdio 用:name → secret ref(對應 mcp_server_secrets.ref_name)
    env_json: Mapped[Optional[dict[str, str]]] = mapped_column(JSON, nullable=True)
    # http 用:header name → secret ref
    headers_json: Mapped[Optional[dict[str, str]]] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # 預設 True:外部 server 不可信。Admin 對 trusted server 可關閉。
    requires_confirmation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Casbin permission key;預設 'mcp.use'(實際是否定義在 catalog 內由部署決定;
    # 沒對應的 perm code 時 service 層退回 superuser-only,不直接放行)。
    casbin_permission: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    last_health: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown"
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    created_by: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "name", name="uq_mcp_servers_org_name"
        ),
        CheckConstraint(
            f"transport IN ('{MCP_TRANSPORT_STDIO}','{MCP_TRANSPORT_HTTP}')",
            name="ck_mcp_servers_transport",
        ),
    )


class MCPServerSecret(Base):
    """secret 用 EncryptedString 自動加解密 — service 層拿到的永遠是明文。"""

    __tablename__ = "mcp_server_secrets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    server_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("mcp_servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 在 env_json / headers_json 內被引用的 key 名(例:"GITHUB_TOKEN")
    ref_name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Optional[str]] = mapped_column(
        EncryptedString(2048), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "server_id", "ref_name", name="uq_mcp_secret_server_ref"
        ),
    )


class MCPToolCache(Base):
    """list_tools 結果快取;由 refresh_tools_cache 主動寫入。

    chat 每輪不必再去打 MCP server 重新 enumerate(stdio 場景特別慢);
    server 設定改動時 service 層會自動清掉舊 cache。
    """

    __tablename__ = "mcp_tools_cache"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    server_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("mcp_servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    input_schema: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "server_id", "tool_name", name="uq_mcp_tool_server_name"
        ),
    )
