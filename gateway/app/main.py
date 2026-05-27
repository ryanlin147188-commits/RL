"""AutoTest API Gateway — Commit 1 MVP 骨架。

職責(Commit 1):
* CORS handling(取代 backend CORSMiddleware 的對外入口角色)
* X-Request-Id 注入(跨服務 trace 用)
* JWT 驗證(/api/* 公路 + public path 白名單,跟 backend 一模一樣的規則)
* HMAC 簽 ``X-Gateway-Verified`` 給 backend short-circuit
* HTTP forward + streaming(/api/* /pics/* /results/*)
* 自己的 ``/healthz`` ``/readyz``;沒裝 prometheus 之前 ``/metrics`` 先 placeholder

Commit 2 加 WebSocket /ws/* proxy;Commit 3 加限速 + 熔斷;Commit 4 加 API key
+ structlog + prometheus。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, PlainTextResponse

from .auth import AuthError, is_public_path, verify_jwt, verify_api_key, mint_short_jwt
from .circuit_breaker import all_status, init_breakers
from .config import settings
from .http_proxy import close_client, forward_request, get_client
from .middleware import AccessLogMiddleware, RequestIdMiddleware
from .rate_limit import enforce_rate_limit
from .routes_config import RoutesConfig, load_routes
from .ws_proxy import proxy_websocket

# 啟動時讀一次 routes.yaml(Commit 3 加),routes_cfg 全域可變(reload 用)
routes_cfg: RoutesConfig = RoutesConfig()

# v1.1.10 Commit 4:接 structlog JSON + Prometheus instrumentator
from .observability import init_logging
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

init_logging()
_log = logging.getLogger("gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _log.info("gateway starting up: backend=%s", settings.backend_url)
    # 讀 routes.yaml + init circuit breakers
    global routes_cfg
    routes_cfg = load_routes(settings.routes_yaml_path)
    init_breakers(routes_cfg.circuit_breakers)
    # Warm up httpx client
    await get_client()
    yield
    _log.info("gateway shutting down")
    await close_client()


app = FastAPI(
    title="AutoTest API Gateway",
    version="1.1.15",
    docs_url=None,   # gateway 不暴露 swagger(backend 的 /api/docs 才是真實 schema)
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

# Prometheus 自動 instrumentation:HTTP latency / status by method+path
# 不 expose 它的 /metrics(我們自己 export,內含 gateway_* gauges)
Instrumentator(
    should_group_status_codes=True,
    should_ignore_untemplated=True,
    excluded_handlers=["/healthz", "/readyz", "/metrics"],
).instrument(app)

# ── Middleware 順序 ─────────────────────────────────────────────────
# Starlette add_middleware 後 add 的先執行(reversed wrap)。
# 期望執行順序(從外到內):CORS → RequestId → AccessLog → AuthCheck → ...
# 所以 add 順序要反過來(Auth 不是 middleware,在 route handler 內 call)。
app.add_middleware(AccessLogMiddleware)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id"],
)


# ── 自身 endpoint ────────────────────────────────────────────────
@app.get("/healthz", include_in_schema=False)
async def healthz():
    """Gateway 自身存活;不探 backend。"""
    return {"status": "ok", "service": "gateway"}


@app.get("/readyz", include_in_schema=False)
async def readyz():
    """探 backend ``/healthz`` 一次;backend 掛了就回 503。"""
    client = await get_client()
    try:
        r = await client.get("/healthz", timeout=httpx.Timeout(3.0))
        if r.status_code == 200:
            return {"status": "ok", "service": "gateway", "backend": "ok"}
        return JSONResponse(
            {"status": "degraded", "backend_status": r.status_code},
            status_code=503,
        )
    except Exception as e:
        return JSONResponse(
            {"status": "down", "error": str(e)},
            status_code=503,
        )


@app.get("/metrics", include_in_schema=False)
async def metrics():
    """Prometheus 指標(default Python proc metrics + 自家 4 個 gateway_*)。"""
    return PlainTextResponse(
        generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST,
    )


@app.get("/gateway/status", include_in_schema=False)
async def gateway_status():
    """Ops 看的 internal status — 含熔斷器當前狀態(Commit 3 加)。"""
    return {
        "backend_url": settings.backend_url,
        "shared_secret_configured": bool(settings.gateway_backend_shared_secret),
        "cors_origins": settings.allowed_origins_list,
        "version": "1.1.10",
        "default_rate_limit": routes_cfg.default_rate_limit,
        "rule_count": len(routes_cfg.routes),
        "circuit_breakers": all_status(),
    }


# ── 驗證 helper ──────────────────────────────────────────────────
async def _check_auth(request: Request) -> dict | None:
    """跑 JWT 或 API key 驗證;public path 直接 None pass through。

    Auth 順序:
    1. ``X-API-Key`` 存在 → 打 backend verify endpoint,mint 短命 JWT 注進 header
    2. 否則照常從 Authorization / cookie / query 取 JWT
    3. public path 都失敗 → 也 None pass through(讓 backend 自己處理)

    Raise AuthError 由上層 catch 轉 401。
    """
    path = request.url.path
    # API Key path
    # 注意:scope 內 header 列表的 mutation 在 BaseHTTPMiddleware 包裝後不一定
    # 會被下游(_filter_request_headers 內的 request.headers iteration)看到 —
    # Starlette Headers 內部會 ``self._list = list(scope["headers"])`` copy 一份。
    # 改用 ``request.state.gateway_auth_override`` 攜帶 minted JWT,
    # forward_request 看到就把 X-API-Key 拿掉 + 把 Authorization 換成這個 JWT。
    api_key = request.headers.get("x-api-key") or request.headers.get("X-API-Key")
    if api_key:
        client = await get_client()
        payload = await verify_api_key(api_key, client)
        if payload:
            jwt = mint_short_jwt(payload, ttl_seconds=300)
            request.state.gateway_auth_override = f"Bearer {jwt}"
            return payload
        # API key 無效 → 直接 401(不要 fallback 到 JWT 路徑,避免暴力測試)
        raise AuthError("API key 無效或已撤銷", code="api_key_invalid")

    if is_public_path(path):
        # public path 仍嘗試 decode 一下(失敗不擋,讓 backend 處理)
        try:
            return await verify_jwt(request)
        except AuthError:
            return None
    # 非 public 一定要有效 token
    return await verify_jwt(request)


# ── Forward routes ──────────────────────────────────────────────
@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_api(request: Request, path: str):
    # CORS OPTIONS 預檢 CORSMiddleware 已經攔,不會到這
    # 1) 找 route rule(rate_limit + circuit_group)
    rule = routes_cfg.match(request.method, request.url.path)
    rate = rule.rate_limit or routes_cfg.default_rate_limit
    # 2) 限速先擋(連 JWT 都不用看,擋掉就省 backend 一次)
    rl_resp = await enforce_rate_limit(request, rate)
    if rl_resp is not None:
        return rl_resp
    # 3) JWT / API key auth
    try:
        payload = await _check_auth(request)
    except AuthError as e:
        # Prometheus counter
        try:
            from .observability import auth_failures
            auth_failures.labels(reason=e.code).inc()
        except Exception:
            pass
        return JSONResponse(
            {"detail": e.detail, "code": e.code},
            status_code=401,
        )
    # 4) forward + circuit breaker
    return await forward_request(
        request, payload=payload, stream=False, circuit_group=rule.circuit_group,
    )


@app.api_route(
    "/pics/{path:path}",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def proxy_pics(request: Request, path: str):
    # /pics/ 走 backend artifact route(它自己有 scoped token / signature 驗證)
    # gateway 直 stream 不驗 JWT
    return await forward_request(request, payload=None, stream=True)


@app.api_route(
    "/results/{path:path}",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def proxy_results(request: Request, path: str):
    return await forward_request(request, payload=None, stream=True)


# ── WebSocket proxy(Commit 2)──────────────────────────────────
@app.websocket("/ws/{path:path}")
async def proxy_ws(websocket: WebSocket, path: str):
    """WebSocket 反代 — 驗 JWT 後雙向 pipe 到 backend。

    現有 ``/ws/executions/{task_id}/logs`` 沒驗 token 是個漏洞,gateway 把
    JWT 驗證集中到這裡,backend 端那條路徑接 X-Gateway-* 信任 header 就好。
    """
    await proxy_websocket(websocket, path)
