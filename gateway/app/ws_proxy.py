"""WebSocket reverse proxy:gateway → backend。

Commit 2 修兩件事:
1. 加 WebSocket auth(現有 ``/ws/executions/{task_id}/logs`` 第一行就
   ``await websocket.accept()``,任何人知道 task_id 就能 listen — 重大漏洞)。
2. 跟 HTTP 一樣注 X-Gateway-* header forward 給 backend。

實作:
* 從以下三個來源任一取 JWT(瀏覽器原生 ``new WebSocket()`` API 不能設
  Authorization header):
    1. ``?access_token=<jwt>`` query param(SPA 用這條)
    2. ``sec-websocket-protocol: autotest.jwt.<jwt>`` subprotocol
    3. Cookie ``access_token``(瀏覽器自動帶,但要看上游 nginx 有沒 forward)
* 驗證失敗 → ``websocket.close(code=4401, reason="unauthorized")`` 不轉發。
* 通過後用 ``websockets.connect()`` 連到 backend,雙向 pipe;任一端斷
  另一端跟著 close。
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlencode

import websockets
from fastapi import WebSocket
from websockets.exceptions import ConnectionClosed

from .auth import AuthError, decode_token, is_jwt_revoked, sign_gateway_request
from .config import settings

_log = logging.getLogger("gateway.ws")

# WebSocket close codes(RFC 6455 4000-4999 是 app-specific)
_WS_CLOSE_UNAUTHORIZED = 4401
_WS_CLOSE_BACKEND_UNREACHABLE = 4502


def _extract_ws_token(websocket: WebSocket) -> str | None:
    """從 query / subprotocol / cookie 取 JWT。"""
    # 1. query param
    tk = websocket.query_params.get("access_token")
    if tk:
        return tk
    # 2. sec-websocket-protocol — 格式 "autotest.jwt.<jwt>"
    proto = websocket.headers.get("sec-websocket-protocol", "")
    for item in proto.split(","):
        item = item.strip()
        if item.startswith("autotest.jwt."):
            return item[len("autotest.jwt."):]
    # 3. cookie(瀏覽器原生 WS 預設會帶,但要看 nginx forward)
    ck = websocket.cookies.get("access_token")
    if ck:
        return ck
    return None


def _build_backend_ws_url(path: str, query: str) -> str:
    """把 ``http://backend:8000`` base 轉成 ``ws://backend:8000/ws/...``。"""
    base = settings.backend_url.replace("http://", "ws://").replace("https://", "wss://")
    url = f"{base}{path}"
    if query:
        url = f"{url}?{query}"
    return url


async def _pipe(reader, writer, direction: str):
    """單向 frame 轉發。任一端收到 close 就拋出 ConnectionClosed,被 gather cancel 對面。

    direction 對應:
      * ``"up"``  → client → backend(reader=starlette WebSocket, writer=websockets client)
      * ``"down"`` → backend → client(reader=websockets client, writer=starlette WebSocket)

    starlette WebSocket 用 ``.receive()``(回 dict);websockets client 用 ``.recv()``(直回 str/bytes)。
    """
    try:
        while True:
            if direction == "up":
                # client → backend:reader 是 starlette WebSocket
                msg = await reader.receive()
                # starlette receive() 回 dict {type, text|bytes}
                if isinstance(msg, dict):
                    if msg.get("type") == "websocket.receive":
                        if msg.get("text") is not None:
                            await writer.send(msg["text"])
                        elif msg.get("bytes") is not None:
                            await writer.send(msg["bytes"])
                    elif msg.get("type") == "websocket.disconnect":
                        return
            else:
                # backend → client:reader 是 websockets client
                msg = await reader.recv()
                # websockets recv() 直回 str|bytes
                if isinstance(msg, (bytes, bytearray)):
                    await writer.send_bytes(msg)
                else:
                    await writer.send_text(str(msg))
    except (ConnectionClosed, asyncio.CancelledError):
        return
    except Exception as e:
        _log.warning("ws pipe %s error: %s", direction, e)


async def proxy_websocket(client_ws: WebSocket, path: str) -> None:
    """主入口:接 client WS,驗 token,連 backend WS,雙向 pipe。"""
    # 1) 認證 — accept 之前驗,失敗直接 close(4401)
    token = _extract_ws_token(client_ws)
    if not token:
        await client_ws.close(code=_WS_CLOSE_UNAUTHORIZED, reason="missing access token")
        _log.warning("ws auth fail (no token): %s", client_ws.url.path)
        return
    try:
        payload = decode_token(token)
    except AuthError as e:
        await client_ws.close(code=_WS_CLOSE_UNAUTHORIZED, reason=e.code)
        _log.warning("ws auth fail (%s): %s", e.code, client_ws.url.path)
        return
    # v1.1.13:WS 連線在 accept 之前也要查 revocation,避免長連線繞過撤銷。
    if await is_jwt_revoked(payload.get("jti")):
        await client_ws.close(code=_WS_CLOSE_UNAUTHORIZED, reason="token_revoked")
        _log.warning("ws auth fail (token_revoked): %s", client_ws.url.path)
        return

    # 2) 組 upstream URL + 注 X-Gateway-* extra headers
    upstream_query = str(client_ws.url.query or "")
    upstream_url = _build_backend_ws_url(f"/ws/{path}", upstream_query)

    sub = payload.get("sub", "")
    sig, ts = sign_gateway_request("GET", f"/ws/{path}", sub)
    extra_headers: list[tuple[str, str]] = []
    if sig:
        extra_headers.extend([
            ("X-Gateway-Verified", sig),
            ("X-Gateway-Timestamp", str(ts)),
            ("X-Gateway-Sub", sub),
            ("X-Gateway-User", payload.get("username") or sub),
        ])
        if payload.get("org_id"):
            extra_headers.append(("X-Gateway-Org", str(payload["org_id"])))
        if payload.get("is_superuser"):
            extra_headers.append(("X-Gateway-Is-Superuser", "1"))

    # 3) 連 backend WS
    try:
        upstream = await websockets.connect(
            upstream_url,
            extra_headers=extra_headers,
            open_timeout=10,
            close_timeout=5,
            max_size=None,   # 不限制 frame 大小(executions log 偶爾大 chunk)
        )
    except Exception as e:
        await client_ws.close(code=_WS_CLOSE_BACKEND_UNREACHABLE, reason="backend unreachable")
        _log.warning("ws connect to backend failed: %s url=%s", e, upstream_url)
        return

    # 4) accept client + 雙向 pipe;任一斷 cancel 對面
    await client_ws.accept()
    _log.info("ws proxy established: %s sub=%s", path, sub)

    upstream_task = asyncio.create_task(_pipe(upstream, client_ws, "down"))
    client_task = asyncio.create_task(_pipe(client_ws, upstream, "up"))
    done, pending = await asyncio.wait(
        {upstream_task, client_task}, return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
    try:
        await upstream.close()
    except Exception:
        pass
    try:
        await client_ws.close()
    except Exception:
        pass
    _log.info("ws proxy closed: %s", path)
