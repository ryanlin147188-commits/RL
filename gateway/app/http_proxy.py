"""HTTP reverse proxy:gateway → backend。

* :func:`forward_request` — 主入口,把 incoming starlette ``Request`` 轉成
  ``httpx.AsyncClient`` 呼叫,完整透傳 headers / body / cookies / multipart。
* 串流:``/pics/*`` 跟 ``/results/*`` 是檔案串流(可能是 MB 級截圖、影片、trace
  zip),用 ``client.stream() + StreamingResponse`` 避免整檔載進記憶體。
* Set-Cookie:Backend 可能 set 多個 cookie(access_token + refresh_token +
  active_org_cookie),httpx 預設把同名 header 合併,我們用
  ``response.headers.raw`` 拿原始 list 一條條 forward。
* X-Gateway-* header 注入:讓 backend 短路驗證。
* Idempotent retry:由 httpx Transport ``retries=2`` 處理 — 只對 connect
  失敗 retry,server-side error 不會;這刻意設計成不在 gateway 重試 5xx,避免
  非 idempotent POST 被誤雙寫。

Commit 3 會在 :func:`forward_request` 加 purgatory 熔斷器包裝。
"""
from __future__ import annotations

from typing import Any, Optional

import httpx
from fastapi import Request
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

from .auth import sign_gateway_request
from .circuit_breaker import get_breaker
from .config import settings


# ── HTTP client(模組單例,連線池共享)──────────────────────────
_transport = httpx.AsyncHTTPTransport(retries=2)
_client: Optional[httpx.AsyncClient] = None


async def get_client() -> httpx.AsyncClient:
    """Lazy initialise;第一次 request 才建,避免 import time side effect。"""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.backend_url,
            transport=_transport,
            timeout=httpx.Timeout(60.0, connect=10.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            # OIDC callback 的 302 必須讓瀏覽器收到,gateway 不能在這 follow
            follow_redirects=False,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ── Hop-by-hop headers(RFC 7230 §6.1)─────────────────────────
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    # content-length 由 httpx 重算,不複製
    "content-length",
})


def _filter_request_headers(request: Request) -> dict[str, str]:
    out = {}
    for k, v in request.headers.items():
        if k.lower() in _HOP_BY_HOP:
            continue
        out[k] = v
    return out


def _build_gateway_headers(
    payload: Optional[dict[str, Any]], path: str, method: str
) -> dict[str, str]:
    """把 JWT payload 變成 X-Gateway-* header,讓 backend 短路驗證。

    沒 JWT(public path)→ 不送 short-circuit header,backend 走原流程。
    沒設 shared secret → 也不送(backend 沒辦法驗 HMAC)。
    """
    if not payload:
        return {}
    sub = payload.get("sub", "")
    if not sub:
        return {}
    sig, ts = sign_gateway_request(method, path, sub)
    if not sig:
        return {}
    headers = {
        "X-Gateway-Verified": sig,
        "X-Gateway-Timestamp": str(ts),
        "X-Gateway-Sub": sub,
        "X-Gateway-User": payload.get("username") or sub,
    }
    if payload.get("org_id"):
        headers["X-Gateway-Org"] = str(payload["org_id"])
    if payload.get("is_superuser"):
        headers["X-Gateway-Is-Superuser"] = "1"
    return headers


def _build_response_raw_headers(upstream: httpx.Response) -> list[tuple[bytes, bytes]]:
    """從 httpx response 拿 raw headers,過濾 hop-by-hop,保留多 Set-Cookie。"""
    out: list[tuple[bytes, bytes]] = []
    for name, value in upstream.headers.raw:
        if name.decode("ascii", errors="ignore").lower() in _HOP_BY_HOP:
            continue
        out.append((name, value))
    return out


# ── 主入口 ───────────────────────────────────────────────────────
async def forward_request(
    request: Request,
    *,
    payload: Optional[dict[str, Any]] = None,
    stream: bool = False,
    circuit_group: str = "default",
) -> Response:
    """把 incoming request forward 給 backend,回 starlette Response。

    熔斷器:OPEN 狀態直接回 503,不打 backend;CLOSED / HALF_OPEN 才 forward。
    上游 5xx / connection error → record_failure;2xx-4xx → record_success。
    """
    breaker = get_breaker(circuit_group)
    if not breaker.allow_request():
        from starlette.responses import JSONResponse
        return JSONResponse(
            {
                "detail": "Service temporarily unavailable",
                "code": "circuit_open",
                "circuit_group": circuit_group,
            },
            status_code=503,
            headers={"Retry-After": "30"},
        )

    client = await get_client()
    method = request.method
    path = request.url.path
    query = request.url.query
    upstream_path = path + (f"?{query}" if query else "")

    fwd_headers = _filter_request_headers(request)
    # API Key path:request.state.gateway_auth_override 是 _check_auth mint 的
    # 短命 JWT。把原始 X-API-Key 拿掉(backend 不用),Authorization 設成新 JWT。
    auth_override = getattr(request.state, "gateway_auth_override", None)
    if auth_override:
        fwd_headers.pop("x-api-key", None)
        fwd_headers.pop("X-API-Key", None)
        fwd_headers["authorization"] = auth_override
    fwd_headers.setdefault("x-forwarded-proto", request.url.scheme)
    fwd_headers.setdefault("x-forwarded-host", request.headers.get("host", ""))
    client_host = request.client.host if request.client else ""
    if client_host and "x-forwarded-for" not in (h.lower() for h in fwd_headers):
        fwd_headers["x-forwarded-for"] = client_host
    rid = getattr(request.state, "request_id", None)
    if rid:
        fwd_headers["x-request-id"] = rid
    fwd_headers.update(_build_gateway_headers(payload, path, method))

    # Body forward:method 沒 body 就 None;有 body 就用 request.stream() async iter
    body_iter = None
    if method not in ("GET", "HEAD"):
        body_iter = request.stream()

    try:
        if stream:
            # /pics/ /results/ — streaming response
            req = client.build_request(
                method, upstream_path, headers=fwd_headers, content=body_iter,
            )
            upstream = await client.send(req, stream=True)
            resp_headers = _build_response_raw_headers(upstream)

            async def _close():
                await upstream.aclose()

            sr = StreamingResponse(
                upstream.aiter_raw(),
                status_code=upstream.status_code,
                background=BackgroundTask(_close),
            )
            sr.raw_headers = resp_headers
            # streaming response 的 status code 在 header 就能判斷;5xx 視為失敗
            if upstream.status_code >= 500:
                breaker.record_failure()
            else:
                breaker.record_success()
            return sr

        upstream = await client.request(
            method, upstream_path, headers=fwd_headers, content=body_iter,
        )
        if upstream.status_code >= 500:
            breaker.record_failure()
        else:
            breaker.record_success()
        resp = Response(content=upstream.content, status_code=upstream.status_code)
        resp.raw_headers = _build_response_raw_headers(upstream)
        return resp
    except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as e:
        # 連線錯誤 / timeout 也算失敗(connection refused / DNS failure 等)
        breaker.record_failure()
        from starlette.responses import JSONResponse
        return JSONResponse(
            {"detail": f"Backend unreachable: {e.__class__.__name__}", "code": "upstream_error"},
            status_code=502,
        )
