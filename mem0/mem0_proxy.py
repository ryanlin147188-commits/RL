"""mem0 sidecar proxy.

PR2 範圍:接 mem0ai library + 5 個路由(add/search/list/delete/delete_all),強制
user_id 注入,LRU cache 重用 Memory 物件。

設計 invariants(plan §4):
- 對外只允許 docker network 內部呼叫 — port 不對 host 公開
- 除 /healthz 外,所有路由必須驗 X-Sidecar-Auth header(== env MEM0_SIDECAR_AUTH_TOKEN)
- user_id 從 request body 帶,proxy 強制把 user_id 餵進 mem0 lib 的 user_id 參數
  (mem0 lib 走 metadata filter 隔離,client 自帶 metadata.user_id 會被擦掉)
- delete_memory 先 verify ownership — 拿 fact 看 metadata.user_id 才能刪
- LLM/embedder key 從 request body 帶,proxy 用完即丟,絕不寫 disk;
  Memory 物件 LRU cache 5 min idle TTL,key 用 sha256 不 leak plaintext
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any, Optional

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from mem0 import Memory
from pydantic import BaseModel, Field

LOG = logging.getLogger("mem0.proxy")

# ── Config(env)─────────────────────────────────────────────────────
LISTEN_HOST = os.environ.get("MEM0_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("MEM0_PORT", "7900"))
SIDECAR_AUTH_TOKEN = os.environ.get("MEM0_SIDECAR_AUTH_TOKEN", "")

PG_HOST = os.environ.get("MEM0_PG_HOST", "mem0-postgres")
PG_PORT = int(os.environ.get("MEM0_PG_PORT", "5432"))
PG_USER = os.environ.get("MEM0_PG_USER", "mem0")
PG_PASSWORD = os.environ.get("MEM0_PG_PASSWORD", "")
PG_DB = os.environ.get("MEM0_PG_DB", "mem0")
# 全 user 共用一個 collection — 隔離靠 metadata.user_id;mem0 lib 內 user_id 自動
# 變 metadata 欄位。多 collection 反而難管 schema。
PG_COLLECTION = os.environ.get("MEM0_PG_COLLECTION", "mem0_facts")
# 對齊 OpenAI text-embedding-3-small 預設(1536)。embed 模型若改 3-large(3072),
# 需要重起 sidecar + 改 collection — schema migration 是 mem0 lib own 的職責,
# 我們用 env 鎖死避免動態切。
EMBEDDING_DIMS = int(os.environ.get("MEM0_EMBEDDING_DIMS", "1536"))

CACHE_MAX = int(os.environ.get("MEM0_CACHE_MAX", "50"))
CACHE_TTL_SEC = int(os.environ.get("MEM0_CACHE_TTL_SEC", "300"))
# Per-user LLM config cache(MCP tool path 用)— TTL 對齊既有 MemoryCache 5min,
# Backend 在 token rotation 時主動 push update,搭配 TTL 雙重保證新鮮度
LLM_CONFIG_CACHE_TTL_SEC = int(os.environ.get("MEM0_LLM_CFG_TTL_SEC", "300"))

MEM0_PROXY_VERSION = "0.3.0-mcp"

if not SIDECAR_AUTH_TOKEN:
    print("[mem0] FATAL: MEM0_SIDECAR_AUTH_TOKEN env not set", file=sys.stderr)
    sys.exit(1)
if not PG_PASSWORD:
    print("[mem0] FATAL: MEM0_PG_PASSWORD env not set", file=sys.stderr)
    sys.exit(1)


# ── pg ping(healthz)────────────────────────────────────────────────
def _pg_conn_string() -> str:
    return (
        f"host={PG_HOST} port={PG_PORT} user={PG_USER} "
        f"password={PG_PASSWORD} dbname={PG_DB} connect_timeout=2"
    )


def _ping_pg() -> tuple[bool, Optional[str]]:
    try:
        with psycopg.connect(_pg_conn_string()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True, None
    except psycopg.OperationalError as e:
        return False, f"pg_unreachable: {type(e).__name__}: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"pg_error: {type(e).__name__}: {e}"


# ── Memory 物件 LRU cache ───────────────────────────────────────────
class MemoryCache:
    """Thread-safe LRU + TTL cache。

    Key = sha256(user_id + llm_config + embedder_config),不 leak plaintext key。
    Value = (Memory 物件, last_used_ts)。
    """

    def __init__(self, max_size: int, ttl: int):
        self._d: OrderedDict[str, tuple[Memory, float]] = OrderedDict()
        self._lock = threading.Lock()
        self.max_size = max_size
        self.ttl = ttl

    def _gc_stale(self, now: float) -> None:
        stale = [k for k, (_, ts) in self._d.items() if now - ts > self.ttl]
        for k in stale:
            self._d.pop(k, None)

    def get_or_create(self, key: str, factory) -> Memory:
        now = time.monotonic()
        with self._lock:
            self._gc_stale(now)
            cached = self._d.get(key)
            if cached:
                # LRU bump
                self._d.move_to_end(key)
                self._d[key] = (cached[0], now)
                return cached[0]
        # 建 Memory(慢:首次連 pgvector + create LLM/embedder client)放鎖外
        mem = factory()
        with self._lock:
            self._d[key] = (mem, time.monotonic())
            while len(self._d) > self.max_size:
                evicted_key, _ = self._d.popitem(last=False)
                LOG.info("memory cache LRU evict key=%s", evicted_key[:8])
        return mem

    def stats(self) -> dict:
        with self._lock:
            return {"size": len(self._d), "max_size": self.max_size, "ttl_sec": self.ttl}


_cache = MemoryCache(CACHE_MAX, CACHE_TTL_SEC)


# ── Per-user LLM config cache(MCP tool path 用)─────────────────────
# Backend 透過 admin endpoint push;tool 從 contextvar 拿 user_id 後讀這個 cache。
# Plaintext key 永不流經 MCP layer / Hermes context — 走 backend ↔ sidecar
# admin endpoint 的 X-Sidecar-Auth 驗過的內網路徑。
class LlmConfigCache:
    """Per-user LLM config cache,thread-safe + TTL。"""

    def __init__(self, ttl: int):
        self._d: dict[str, tuple[dict, dict, float]] = {}
        # value = (llm_config, embedder_config, last_updated_ts)
        self._lock = threading.Lock()
        self.ttl = ttl

    def put(self, user_id: str, llm_config: dict, embedder_config: dict) -> None:
        with self._lock:
            self._d[user_id] = (llm_config, embedder_config, time.monotonic())

    def get(self, user_id: str) -> Optional[tuple[dict, dict]]:
        now = time.monotonic()
        with self._lock:
            entry = self._d.get(user_id)
            if entry is None:
                return None
            llm, emb, ts = entry
            if now - ts > self.ttl:
                # 過期 → 清掉,讓 caller 知道要請 backend 重 push
                self._d.pop(user_id, None)
                return None
            return llm, emb

    def delete(self, user_id: str) -> bool:
        with self._lock:
            return self._d.pop(user_id, None) is not None

    def stats(self) -> dict:
        with self._lock:
            return {"size": len(self._d), "ttl_sec": self.ttl}


_llm_config_cache = LlmConfigCache(LLM_CONFIG_CACHE_TTL_SEC)


# 由 ASGI middleware(對 /mcp/* 路徑)塞入的 user_id;MCP tool function 從 contextvar
# 讀。FastMCP Context 沒有直接給 raw HTTP headers — 用 contextvar 是最乾淨的橋。
_current_mcp_user_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "mcp_user_id", default=None,
)


def _build_memory_config(llm_config: dict, embedder_config: dict) -> dict:
    """從 backend 帶來的 llm/embedder config 拼成 mem0.Memory.from_config 接受的 dict。"""
    return {
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "dbname": PG_DB,
                "collection_name": PG_COLLECTION,
                "embedding_model_dims": EMBEDDING_DIMS,
                "user": PG_USER,
                "password": PG_PASSWORD,
                "host": PG_HOST,
                "port": PG_PORT,
                "diskann": False,
                "hnsw": True,
            },
        },
        "llm": llm_config,
        "embedder": embedder_config,
    }


# LLM provider 的錯誤訊息常常把 plaintext key 印出來(例:OpenAI 401 回的
# `Incorrect API key provided: sk-XXX...`)。proxy 在把錯誤透傳給 backend 之前
# 先把所有 key-shape 字串 redact,避免 502 detail 透過 backend log 外洩。
_KEY_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{6,}"),                    # OpenAI / Anthropic / DeepSeek / OpenRouter
    re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}"),  # JWT-like
    re.compile(r"Bearer\s+[A-Za-z0-9_.\-]+", re.IGNORECASE),
]


def _redact_secrets(text: str) -> str:
    if not text:
        return text
    out = text
    for pat in _KEY_PATTERNS:
        out = pat.sub("<redacted>", out)
    return out


def _cache_key(user_id: str, llm_config: dict, embedder_config: dict) -> str:
    blob = json.dumps(
        {"u": user_id, "l": llm_config, "e": embedder_config},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _get_memory(user_id: str, llm_config: dict, embedder_config: dict) -> Memory:
    key = _cache_key(user_id, llm_config, embedder_config)

    def _build():
        cfg = _build_memory_config(llm_config, embedder_config)
        return Memory.from_config(cfg)

    return _cache.get_or_create(key, _build)


# ── Pydantic schemas ───────────────────────────────────────────────
class LlmConfig(BaseModel):
    provider: str  # 'openai' / 'anthropic' / 'gemini' / 'deepseek' / ...
    config: dict


class EmbedderConfig(BaseModel):
    provider: str  # 'openai' / 'huggingface' / 'gemini' / ...
    config: dict


class AddRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=200)
    messages: Any  # list[dict{role,content}] 或 str
    llm_config: LlmConfig
    embedder_config: EmbedderConfig
    metadata: Optional[dict] = None
    infer: bool = True


class SearchRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=200)
    query: str = Field(..., min_length=1)
    llm_config: LlmConfig
    embedder_config: EmbedderConfig
    top_k: int = Field(default=5, ge=1, le=100)
    threshold: Optional[float] = 0.3


class ListRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=200)
    llm_config: LlmConfig
    embedder_config: EmbedderConfig
    limit: int = Field(default=50, ge=1, le=200)


class DeleteRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=200)
    llm_config: LlmConfig
    embedder_config: EmbedderConfig


class DeleteAllRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=200)
    llm_config: LlmConfig
    embedder_config: EmbedderConfig
    confirm: bool


# ── App ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    LOG.info(
        "mem0 proxy starting host=%s port=%d pg=%s:%d/%s collection=%s "
        "cache_max=%d cache_ttl=%ds version=%s",
        LISTEN_HOST, LISTEN_PORT, PG_HOST, PG_PORT, PG_DB, PG_COLLECTION,
        CACHE_MAX, CACHE_TTL_SEC, MEM0_PROXY_VERSION,
    )
    # FastMCP streamable_http_app() 要求其 session_manager 在 ASGI lifespan 內
    # 手動 enter — 否則 mount 到 FastAPI 後 endpoint 會 raise
    # `Task group is not initialized`(mcp 1.27.x 行為)。我們在 FastMCP
    # sub-app 的 mount point 之後初始化,進入 manager 的 run() async ctx。
    async with _mcp_server.session_manager.run():
        yield
    LOG.info("mem0 proxy shutting down")


app = FastAPI(
    title="mem0 sidecar proxy",
    description="Per-user semantic memory layer for AutoTest AI assistant",
    version=MEM0_PROXY_VERSION,
    lifespan=lifespan,
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """All paths except /healthz must carry X-Sidecar-Auth.

    For /mcp/* paths(MCP HTTP transport),we additionally read X-Mem0-User-Id
    from headers and stash it in the contextvar — FastMCP Context 沒有直接給
    raw HTTP headers,contextvar 是最乾淨的橋。
    """
    if request.url.path == "/healthz":
        return await call_next(request)
    token = request.headers.get("X-Sidecar-Auth", "")
    if not token or token != SIDECAR_AUTH_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if request.url.path.startswith("/mcp"):
        user_id = request.headers.get("X-Mem0-User-Id", "").strip()
        if not user_id:
            return JSONResponse(
                {"error": "missing_user_id",
                 "message": "MCP requests require X-Mem0-User-Id header"},
                status_code=400,
            )
        ctx_token = _current_mcp_user_id.set(user_id)
        try:
            return await call_next(request)
        finally:
            _current_mcp_user_id.reset(ctx_token)
    return await call_next(request)


# ── /healthz ────────────────────────────────────────────────────────
@app.get("/healthz")
async def healthz():
    pg_ok, pg_err = _ping_pg()
    if not pg_ok:
        return JSONResponse(
            {
                "status": "unhealthy",
                "reason": pg_err,
                "version": MEM0_PROXY_VERSION,
                "phase": "PR2",
            },
            status_code=503,
        )
    return {
        "status": "ok",
        "version": MEM0_PROXY_VERSION,
        "phase": "MCP",
        "pg_host": PG_HOST,
        "pg_db": PG_DB,
        "collection": PG_COLLECTION,
        "embedding_dims": EMBEDDING_DIMS,
        "cache": _cache.stats(),
        "llm_config_cache": _llm_config_cache.stats(),
        "mcp_endpoint": "/mcp/mcp",  # FastMCP streamable_http_app default mounts at /mcp inside subapp
    }


# ── Admin:per-user LLM config push/clear ──────────────────────────
class LlmConfigPushRequest(BaseModel):
    llm_config: LlmConfig
    embedder_config: EmbedderConfig


def _validate_user_id(user_id: str) -> None:
    """Path 內 user_id 防 traversal。Backend 端用 `{org}:{username}` 格式
    (見 _mem0_user_id),沒有 / 跟 ..  與一般 path 字元都拒。"""
    if not user_id or len(user_id) > 200:
        raise HTTPException(400, "invalid_user_id")
    if any(c in user_id for c in ("/", "\\", "..", "\x00", " ", "\t", "\n")):
        raise HTTPException(400, "invalid_user_id")


@app.post("/admin/users/{user_id}/llm_config")
async def admin_push_llm_config(user_id: str, req: LlmConfigPushRequest):
    _validate_user_id(user_id)
    _llm_config_cache.put(
        user_id,
        req.llm_config.model_dump(),
        req.embedder_config.model_dump(),
    )
    return {"status": "ok", "user_id": user_id}


@app.delete("/admin/users/{user_id}/llm_config", status_code=204)
async def admin_clear_llm_config(user_id: str):
    _validate_user_id(user_id)
    _llm_config_cache.delete(user_id)
    return JSONResponse(status_code=204, content=None)


# ── 路由 ──────────────────────────────────────────────────────────────
def _strip_user_id_metadata(metadata: Optional[dict]) -> dict:
    """從 client 帶來的 metadata 擦掉 user_id(防止 client 自帶 user_id 偽造身份)。

    user_id 由 mem0 lib 的 user_id 參數注入到 metadata,client 不該也不能自帶。
    """
    out = dict(metadata or {})
    out.pop("user_id", None)
    out.pop("agent_id", None)
    out.pop("run_id", None)
    return out


@app.post("/v1/memory/add")
async def add_memory(req: AddRequest):
    try:
        memory = _get_memory(
            req.user_id, req.llm_config.model_dump(), req.embedder_config.model_dump(),
        )
        meta = _strip_user_id_metadata(req.metadata)
        # mem0.add 是同步 — 內部會 LLM call(可能 1-2s)
        result = memory.add(
            messages=req.messages,
            user_id=req.user_id,
            metadata=meta or None,
            infer=req.infer,
        )
        return {"status": "ok", "result": result}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        LOG.exception("add failed user=%s", req.user_id)
        raise HTTPException(
            status_code=502,
            detail=f"mem0_add_failed: {type(e).__name__}: {_redact_secrets(str(e))[:300]}",
        )


@app.post("/v1/memory/search")
async def search_memory(req: SearchRequest):
    try:
        memory = _get_memory(
            req.user_id, req.llm_config.model_dump(), req.embedder_config.model_dump(),
        )
        # 強制把 user_id 餵進去 — 同 mem0 metadata 隔離
        result = memory.search(
            query=req.query, user_id=req.user_id,
            limit=req.top_k, threshold=req.threshold,
        )
        # mem0 v0.1.x search 回 {results: [...]};我們透傳 results 部分
        if isinstance(result, dict):
            return {"results": result.get("results") or []}
        return {"results": result or []}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        LOG.exception("search failed user=%s", req.user_id)
        raise HTTPException(
            status_code=502,
            detail=f"mem0_search_failed: {type(e).__name__}: {_redact_secrets(str(e))[:300]}",
        )


@app.post("/v1/memory/list")
async def list_memories(req: ListRequest):
    try:
        memory = _get_memory(
            req.user_id, req.llm_config.model_dump(), req.embedder_config.model_dump(),
        )
        result = memory.get_all(user_id=req.user_id, limit=req.limit)
        if isinstance(result, dict):
            return {"results": result.get("results") or []}
        return {"results": result or []}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        LOG.exception("list failed user=%s", req.user_id)
        raise HTTPException(
            status_code=502,
            detail=f"mem0_list_failed: {type(e).__name__}: {_redact_secrets(str(e))[:300]}",
        )


def _verify_ownership(memory: Memory, user_id: str, memory_id: str) -> bool:
    """確認 fact 屬於指定 user — 防止跨 user 刪除。"""
    try:
        fact = memory.get(memory_id)
    except Exception:  # noqa: BLE001
        return False
    if not fact:
        return False
    # mem0 v0.1.x:fact dict 內 user_id 直接 in fact;也檢查 metadata 兩種可能位置
    if isinstance(fact, dict):
        if fact.get("user_id") == user_id:
            return True
        meta = fact.get("metadata") or {}
        if isinstance(meta, dict) and meta.get("user_id") == user_id:
            return True
    return False


# NOTE: 路由註冊順序 — delete_all 必須先於 delete_memory({memory_id}),否則
# FastAPI 會把 `/v1/memory/all` 當成 memory_id="all" 走到 delete_memory 去。
# FastAPI 走「先註冊的先 match」,fixed path 不會自動 priority。
@app.delete("/v1/memory/all")
async def delete_all(req: DeleteAllRequest):
    if not req.confirm:
        raise HTTPException(400, "confirm_required")
    try:
        memory = _get_memory(
            req.user_id, req.llm_config.model_dump(), req.embedder_config.model_dump(),
        )
        # mem0 lib delete_all 走 user_id 過濾 — 不會誤刪別 user 資料
        memory.delete_all(user_id=req.user_id)
        return {"status": "ok", "deleted_user_id": req.user_id}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        LOG.exception("delete_all failed user=%s", req.user_id)
        raise HTTPException(
            status_code=502,
            detail=f"mem0_delete_all_failed: {type(e).__name__}: {_redact_secrets(str(e))[:300]}",
        )


@app.delete("/v1/memory/{memory_id}")
async def delete_memory(memory_id: str, req: DeleteRequest):
    # NOTE: "all" 不會走到這裡(被前面 delete_all route 攔截);保險起見也擋
    if memory_id == "all":
        raise HTTPException(400, "use POST /v1/memory/all for wipe")
    if not memory_id or "/" in memory_id or ".." in memory_id or len(memory_id) > 64:
        raise HTTPException(400, "invalid_memory_id")
    try:
        memory = _get_memory(
            req.user_id, req.llm_config.model_dump(), req.embedder_config.model_dump(),
        )
        if not _verify_ownership(memory, req.user_id, memory_id):
            # 不 leak「memory 存在但屬於別 user」 vs「memory 不存在」差別 — 一律 404
            raise HTTPException(404, "memory_not_found")
        memory.delete(memory_id)
        return JSONResponse(status_code=204, content=None)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        LOG.exception("delete failed user=%s mid=%s", req.user_id, memory_id)
        raise HTTPException(
            status_code=502,
            detail=f"mem0_delete_failed: {type(e).__name__}: {_redact_secrets(str(e))[:300]}",
        )


# ── FastMCP sub-app(讓 Hermes agent 把 mem0 當 MCP tool 用)──────────
# 設計取捨(plan §1.2 + §4):
# - tool signature 只有 query + top_k,LLM 看不到 user_id / api_key
# - user_id 從 contextvar 取(由 auth_middleware 從 X-Mem0-User-Id 注入)
# - llm_config 從 _llm_config_cache 拿(backend admin endpoint push 過)
# - 失敗回 LLM-friendly text(別 raise,讓 LLM 自然繼續對話)
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# DNS rebinding 防護:預設只允許 127.0.0.1,我們 sidecar 只在內網被 hermes
# 用 docker service-name `mem0:7900` 連 — 加白名單。Backend 也可能直連測試用。
_mcp_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["mem0", "mem0:7900", "127.0.0.1", "127.0.0.1:7900",
                   "localhost", "localhost:7900"],
    allowed_origins=["*"],  # 內網限定 + X-Sidecar-Auth 已驗,origin 不額外限
)

_mcp_server = FastMCP(name="mem0-memory", transport_security=_mcp_security)


@_mcp_server.tool()
async def search_memory(query: str, top_k: int = 5) -> str:
    """Search the user's persistent semantic memory for facts they've shared in
    past conversations (e.g. preferences, project context, past decisions).

    Use this tool when the user references something from before that you don't
    have in the current conversation context. Returns a formatted summary of
    the most relevant facts, or "No matching memories" if nothing matches.

    Args:
        query: Natural-language search query (e.g. "testing framework preference")
        top_k: Max number of memories to return (default 5)
    """
    user_id = _current_mcp_user_id.get()
    if not user_id:
        # 不該發生(auth_middleware 已驗 user_id 必填),但保險起見回 friendly
        return "Memory unavailable: user context missing. Please continue the conversation without recall."

    cached = _llm_config_cache.get(user_id)
    if not cached:
        # Backend 還沒 push llm_config 過來 / 過期 — 不能 search;讓 LLM 知道
        return (
            "Memory recall is temporarily unavailable for this session. "
            "Please continue without it; long-term memory will resume on the next session."
        )
    llm_config, embedder_config = cached

    # top_k 邊界保護
    try:
        top_k = max(1, min(20, int(top_k)))
    except (TypeError, ValueError):
        top_k = 5

    try:
        memory = _get_memory(user_id, llm_config, embedder_config)
        result = memory.search(
            query=query, user_id=user_id, limit=top_k,
        )
        hits = (result.get("results") if isinstance(result, dict) else result) or []
    except Exception as e:  # noqa: BLE001
        LOG.warning("mem0 mcp search failed user=%s: %s",
                    user_id, _redact_secrets(str(e))[:200])
        return (
            f"Failed to search memory ({type(e).__name__}). "
            "Please continue without recall."
        )

    if not hits:
        return f'No matching memories found for query: "{query}"'

    # LLM-friendly markdown,plan §1.3 範例
    lines = [f'Found {len(hits)} memories matching "{query}":']
    for h in hits:
        if not isinstance(h, dict):
            continue
        memory_text = h.get("memory") or ""
        ts = h.get("created_at") or h.get("updated_at") or ""
        ts_short = str(ts)[:10] if ts else ""
        if ts_short:
            lines.append(f"- {memory_text} (recorded {ts_short})")
        else:
            lines.append(f"- {memory_text}")
    return "\n".join(lines)


# Mount FastMCP streamable HTTP app under /mcp;auth_middleware 已驗
# X-Sidecar-Auth + 注入 user_id contextvar,所以 sub-app 內可信任 contextvar
app.mount("/mcp", _mcp_server.streamable_http_app())


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    import uvicorn
    uvicorn.run(
        app, host=LISTEN_HOST, port=LISTEN_PORT,
        log_config=None, access_log=False,
    )


if __name__ == "__main__":
    main()
