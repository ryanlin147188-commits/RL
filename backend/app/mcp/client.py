"""MCP ClientSession 包裝層 — Phase 2(streamable HTTP only)。

設計重點:
* 每個 MCP server 一個 ``MCPClient`` instance,內部 lazy 起 ClientSession。
* connection 經由 ``streamablehttp_client(url, headers=...)`` 拉起;進入
  ClientSession 後第一件事是 ``initialize()`` 拿 server capabilities。
* call_tool / list_tools 統一在這層 wrap,timeout 由呼叫端用 ``asyncio.wait_for`` 控。
* stdio transport 留接口但 Phase 2 不啟用 — 接到時 raise ``MCPTransportNotImplemented``。

紅線:
* 任何 outbound HTTP 都不會把 secret 寫進 server log;exception message scrub URL
  query string 與 Authorization header 值。
* MCP server 可能傳回 image / resource block;這層只負責原樣回傳,由
  ``tool_adapter`` 處理截斷 / 包 XML 邊界。
"""
from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any, Optional

log = logging.getLogger(__name__)


class MCPError(Exception):
    """Service / router 層轉 HTTPException 用。"""


class MCPTransportNotImplemented(MCPError):
    """目前不支援的 transport 類別。"""


class MCPConnectionError(MCPError):
    """連線 / initialize 失敗。"""


class MCPToolCallError(MCPError):
    """call_tool 失敗(server 回 error 或網路掛掉)。"""


# 內含 mcp SDK 的 import — 用 lazy import 避免 backend 啟動時尚未裝套件就 crash
def _import_mcp_sdk():
    try:
        from mcp import ClientSession  # type: ignore
        from mcp.client.streamable_http import streamablehttp_client  # type: ignore
    except ImportError as exc:  # pragma: no cover — deps 沒裝才會觸發
        raise MCPConnectionError(
            "mcp SDK 未安裝,請執行 pip install mcp 並重 build backend image"
        ) from exc
    return ClientSession, streamablehttp_client


def _import_mcp_stdio():
    """Phase 3:stdio transport 的 SDK import,獨立一份 lazy 避免 http path 受影響。"""
    try:
        from mcp import ClientSession, StdioServerParameters  # type: ignore
        from mcp.client.stdio import stdio_client  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise MCPConnectionError(
            "mcp SDK 未安裝(stdio path),請執行 pip install mcp 並重 build backend image"
        ) from exc
    return ClientSession, StdioServerParameters, stdio_client


class MCPClient:
    """單一 MCP server 的連線 + RPC 包裝。

    使用方式:用 ``async with MCPClient.from_config(...) as client`` 取得已
    initialize 的 client;``connection_pool`` 會幫你管 lifecycle。
    """

    def __init__(
        self,
        *,
        server_id: str,
        transport: str,
        url: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        command: Optional[str] = None,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
    ):
        self.server_id = server_id
        self.transport = transport
        self.url = url
        self.headers = headers or {}
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self._exit_stack: Optional[AsyncExitStack] = None
        self._session = None  # type: ignore[assignment]

    @classmethod
    async def open(
        cls,
        *,
        server_id: str,
        transport: str,
        url: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        command: Optional[str] = None,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
    ) -> "MCPClient":
        """建一個 client 並 enter 對應 transport 的 async context。"""
        if transport == "http":
            if not url:
                raise MCPConnectionError("http transport 需要 url")
        elif transport == "stdio":
            if not command:
                raise MCPConnectionError("stdio transport 需要 command")
        else:
            raise MCPTransportNotImplemented(
                f"未知 transport:{transport!r}(支援:http / stdio)"
            )

        client = cls(
            server_id=server_id,
            transport=transport,
            url=url,
            headers=headers,
            command=command,
            args=args,
            env=env,
        )
        await client._enter()
        return client

    async def _enter(self) -> None:
        if self.transport == "stdio":
            await self._enter_stdio()
            return
        await self._enter_http()

    async def _enter_http(self) -> None:
        ClientSession, streamablehttp_client = _import_mcp_sdk()
        stack = AsyncExitStack()
        try:
            # streamablehttp_client 回 (read_stream, write_stream, get_session_id)
            transport_ctx = streamablehttp_client(self.url, headers=self.headers)
            read_stream, write_stream, _get_sid = await stack.enter_async_context(
                transport_ctx
            )
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
            self._session = session
            self._exit_stack = stack
        except Exception as exc:  # noqa: BLE001
            await stack.aclose()
            raise MCPConnectionError(
                f"無法連線到 MCP server (id={self.server_id}, http): {type(exc).__name__}"
            ) from exc

    async def _enter_stdio(self) -> None:
        """Phase 3:spawn subprocess (npx / uvx / 任何 stdio MCP) 並 wire 通 stdin/stdout。

        env 是已解密後的明文 — 由 service 層 ``resolve_env`` 處理過再傳進來。
        不會把 env value 寫進日誌(MCPConnectionError 訊息只含 server_id + 例外類別名)。
        """
        ClientSession, StdioServerParameters, stdio_client = _import_mcp_stdio()
        stack = AsyncExitStack()
        try:
            params = StdioServerParameters(
                command=self.command,
                args=self.args,
                env=self.env or None,
            )
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(params)
            )
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
            self._session = session
            self._exit_stack = stack
        except Exception as exc:  # noqa: BLE001
            await stack.aclose()
            raise MCPConnectionError(
                f"無法連線到 MCP server (id={self.server_id}, stdio): {type(exc).__name__}"
            ) from exc

    async def close(self) -> None:
        """關掉 stream + session;失敗時靜默吞掉(避免 close path 噴 exception)。"""
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception:  # noqa: BLE001
                log.exception("MCPClient close failed (server_id=%s)", self.server_id)
            finally:
                self._exit_stack = None
                self._session = None

    async def list_tools(self) -> list[dict[str, Any]]:
        """回傳 normalised tool defs: ``[{name, description, input_schema}, ...]``。"""
        if self._session is None:
            raise MCPConnectionError("MCPClient 尚未初始化")
        result = await self._session.list_tools()
        tools_raw = getattr(result, "tools", None) or []
        out: list[dict[str, Any]] = []
        for t in tools_raw:
            out.append({
                "name": getattr(t, "name", "") or "",
                "description": getattr(t, "description", "") or "",
                # mcp SDK 的 inputSchema 已是 JSON Schema dict
                "input_schema": getattr(t, "inputSchema", None) or {},
            })
        return out

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """呼叫 server 上的 tool,回傳 normalised result。

        Result 結構:
            {
                "is_error": bool,
                "blocks": list[{"type": "text"|"image"|"resource", ...}],
            }
        adapter 自己決定怎麼變成 ``ToolResult.content``。
        """
        if self._session is None:
            raise MCPConnectionError("MCPClient 尚未初始化")
        try:
            result = await self._session.call_tool(name, arguments=arguments)
        except Exception as exc:  # noqa: BLE001
            raise MCPToolCallError(
                f"MCP tool {name} 呼叫失敗:{type(exc).__name__}"
            ) from exc

        is_error = bool(getattr(result, "isError", False))
        content_blocks = getattr(result, "content", None) or []
        normalised: list[dict[str, Any]] = []
        for block in content_blocks:
            btype = getattr(block, "type", "unknown")
            if btype == "text":
                normalised.append({
                    "type": "text",
                    "text": getattr(block, "text", "") or "",
                })
            elif btype == "image":
                normalised.append({
                    "type": "image",
                    "mime_type": getattr(block, "mimeType", "") or "image/png",
                    "data_len": len(getattr(block, "data", "") or ""),
                })
            elif btype == "resource":
                resource = getattr(block, "resource", None)
                normalised.append({
                    "type": "resource",
                    "uri": getattr(resource, "uri", "") if resource else "",
                })
            else:
                normalised.append({"type": btype})
        return {"is_error": is_error, "blocks": normalised}
