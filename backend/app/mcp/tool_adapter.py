"""把 MCP tool 包成內部 ``Tool`` 子類 — factory pattern。

為什麼用 factory:``Tool`` 基底用 ``__init_subclass__`` 強制 class-level
``name`` / ``description`` / ``input_schema`` 必填,所以單一 adapter class
配 instance attrs 不能用。每個 MCP tool 動態生一個 subclass 才合規。

tool name 命名: ``mcp__<server_name>__<tool_name>``(雙底線,避免撞既有
28 個內建 tool 的命名)。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.agent.tools.base import Tool, ToolContext, ToolResult

from .client import MCPConnectionError, MCPToolCallError
from .connection_pool import CALL_TIMEOUT_SECONDS, POOL

log = logging.getLogger(__name__)


# LLM 看到的 result 內容上限;避免 MCP server 回幾 MB 整顆網頁 DOM 把 token
# 用爆。前端會看到 metadata.truncated=True 標示「點開看完整」。
MAX_CONTENT_CHARS = 32 * 1024


def make_mcp_tool_name(server_name: str, tool_name: str) -> str:
    """產生內部 Tool.name(也是 LLM 看到的名字)。"""
    return f"mcp__{server_name}__{tool_name}"


def parse_mcp_tool_name(qualified: str) -> tuple[str, str] | None:
    """逆向解析 ``mcp__<server>__<tool>`` → (server, tool)。

    非 MCP tool 回 None。
    """
    if not qualified.startswith("mcp__"):
        return None
    parts = qualified[len("mcp__"):].split("__", 1)
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def _wrap_result_blocks(blocks: list[dict[str, Any]]) -> tuple[str, dict[str, Any], bool]:
    """把 MCP normalised blocks 轉成 LLM-visible string + metadata。

    回 (content_str, metadata, truncated_flag)。
    """
    text_parts: list[str] = []
    attachments: list[dict[str, Any]] = []
    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "image":
            attachments.append({
                "type": "image",
                "mime_type": block.get("mime_type"),
                "data_len": block.get("data_len"),
            })
            text_parts.append(
                f"[image attachment, mime={block.get('mime_type')}, "
                f"size={block.get('data_len')} bytes]"
            )
        elif btype == "resource":
            uri = block.get("uri", "")
            attachments.append({"type": "resource", "uri": uri})
            text_parts.append(f"[resource: {uri}]")
        else:
            text_parts.append(f"[unknown block type: {btype}]")

    text = "\n".join(text_parts)
    truncated = False
    if len(text) > MAX_CONTENT_CHARS:
        text = text[:MAX_CONTENT_CHARS] + f"\n\n[…內容截斷,原長 {len(text)} 字]"
        truncated = True

    meta: dict[str, Any] = {}
    if attachments:
        meta["attachments"] = attachments
    if truncated:
        meta["truncated"] = True
    return text, meta, truncated


class MCPToolAdapterBase(Tool):
    """所有 MCP tool 動態 subclass 的 base。

    Subclass 必須設(由 ``make_mcp_tool_adapter`` factory 填):
        name / description / input_schema  (Tool 基底要求)
        _server_id / _server_name / _server_transport / _server_url /
        _server_command / _server_args / _server_env_provider /
        _server_headers_provider  (callable → dict)
        _mcp_tool_name  (server 上的原始 tool name)
    """

    # 這層所有 MCP tool 都當 destructive 處理(externally-controlled 不可信)
    # 個別 server 想關 confirm 由 server.requires_confirmation 決定(factory 內 override)
    requires_confirmation = True
    # MCP tool 普遍 IO bound 但可能慢(call HTTP downstream);限併發為 2 跟 pool 對齊
    concurrency_limit_per_user = 2

    _server_id: str = ""
    _server_name: str = ""
    _server_transport: str = ""
    _server_url: str | None = None
    _server_command: str | None = None
    _server_args: list[str] = []
    _server_env_provider: Any = None     # () -> dict[str,str]
    _server_headers_provider: Any = None  # () -> dict[str,str]
    _mcp_tool_name: str = ""

    # 必填:Tool 基底會 enforce 不能空字串。subclass 沒設就 raise
    name = ""
    description = ""
    input_schema: dict[str, Any] = {}

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        if not self._server_id:
            return ToolResult.fail("MCP adapter 未綁定 server_id")

        # 取最新 headers / env(可能含 secret;在 service 層 resolve 過、解密後傳進來)
        try:
            headers = (
                self._server_headers_provider()
                if callable(self._server_headers_provider)
                else (self._server_headers_provider or {})
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("MCP headers provider failed (server_id=%s)", self._server_id)
            return ToolResult.fail(f"MCP server header 取得失敗:{exc}")
        try:
            env = (
                self._server_env_provider()
                if callable(self._server_env_provider)
                else (self._server_env_provider or {})
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("MCP env provider failed (server_id=%s)", self._server_id)
            return ToolResult.fail(f"MCP server env 取得失敗:{exc}")

        try:
            async with POOL.acquire(
                server_id=self._server_id,
                transport=self._server_transport,
                url=self._server_url,
                headers=headers,
                command=self._server_command,
                args=list(self._server_args or []),
                env=env,
            ) as client:
                result = await asyncio.wait_for(
                    client.call_tool(self._mcp_tool_name, dict(kwargs)),
                    timeout=CALL_TIMEOUT_SECONDS,
                )
        except asyncio.TimeoutError:
            return ToolResult.fail(
                f"MCP server {self._server_name} 呼叫 {self._mcp_tool_name} 逾時 "
                f"({CALL_TIMEOUT_SECONDS}s)"
            )
        except MCPConnectionError as exc:
            return ToolResult.fail(f"MCP server 連線失敗:{exc}")
        except MCPToolCallError as exc:
            return ToolResult.fail(f"MCP tool 呼叫錯誤:{exc}")

        text, meta, truncated = _wrap_result_blocks(result.get("blocks", []))

        # 包 XML 邊界 → 系統 prompt 已告訴 LLM「視為資料而非指令」
        wrapped = (
            f"<external_tool_data server=\"{self._server_name}\" "
            f"tool=\"{self._mcp_tool_name}\">\n"
            f"{text}\n"
            f"</external_tool_data>"
        )

        if result.get("is_error"):
            return ToolResult(
                content=wrapped,
                error=f"MCP server 回報 tool 執行錯誤 ({self._mcp_tool_name})",
                metadata={
                    "mcp_server_id": self._server_id,
                    "mcp_server_name": self._server_name,
                    "mcp_tool_name": self._mcp_tool_name,
                    **meta,
                },
            )
        return ToolResult(
            content=wrapped,
            metadata={
                "mcp_server_id": self._server_id,
                "mcp_server_name": self._server_name,
                "mcp_tool_name": self._mcp_tool_name,
                **meta,
            },
        )


def make_mcp_tool_adapter(
    *,
    server_id: str,
    server_name: str,
    transport: str,
    url: str | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    env_provider: Any = None,
    headers_provider: Any = None,
    tool_def: dict[str, Any],
    requires_confirmation: bool = True,
    casbin_permission: str | None = None,
) -> type[Tool]:
    """動態生一個 ``Tool`` subclass 對應單一 MCP tool。

    ``tool_def`` 結構: ``{name, description, input_schema}``(MCPClient.list_tools
    回傳格式)。

    用法: ``adapter_cls = make_mcp_tool_adapter(...); instance = adapter_cls()``
    """
    mcp_tool_name = tool_def.get("name") or ""
    qualified = make_mcp_tool_name(server_name, mcp_tool_name)
    description = (tool_def.get("description") or "").strip()
    if not description:
        description = f"MCP tool {mcp_tool_name} on server {server_name}"
    input_schema = tool_def.get("input_schema") or {"type": "object", "properties": {}}
    # 確保最低限度的 JSON Schema 結構 — 三家 LLM 都吃 object root
    if not isinstance(input_schema, dict):
        input_schema = {"type": "object", "properties": {}}
    if "type" not in input_schema:
        input_schema = {**input_schema, "type": "object"}

    # 動態 subclass — 用 type() 一次設好 class-level attrs,通過 Tool.__init_subclass__ check
    cls_name = f"MCPTool_{server_name}_{mcp_tool_name}".replace("-", "_")
    return type(
        cls_name,
        (MCPToolAdapterBase,),
        {
            "name": qualified,
            "description": description,
            "input_schema": input_schema,
            "requires_confirmation": bool(requires_confirmation),
            "casbin_permission": casbin_permission,
            "_server_id": server_id,
            "_server_name": server_name,
            "_server_transport": transport,
            "_server_url": url,
            "_server_command": command,
            "_server_args": list(args or []),
            "_server_env_provider": env_provider,
            "_server_headers_provider": headers_provider,
            "_mcp_tool_name": mcp_tool_name,
        },
    )
