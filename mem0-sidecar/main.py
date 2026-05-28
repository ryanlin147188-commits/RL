"""mem0 sidecar — FastAPI 包 mem0ai 套件。

設計:
* 不在 sidecar 環境變數塞 LLM key — 每個 request 由 backend 帶該 org 的
  LLM config(reuse RL 的 LlmProviderConfig)。
* sidecar 內部用 LRU cache 把 (org_id, provider, model) → Memory instance 緩存,
  避免每 request 重建 vector store 連線。
* 認證:backend 與 sidecar 共用 ``X-Sidecar-Auth`` shared secret(env
  ``MEM0_SIDECAR_AUTH``);未帶或不對 → 401。
* 不對外暴露 — docker-compose 不 publish port,只在 docker network 內可達。

REST API:
* ``GET /healthz`` 不需 auth — 給 docker healthcheck
* ``POST /v1/memories`` — add 一條(自動 summarize / dedup)
* ``POST /v1/search`` — 語意搜尋(回 top-k 相關)
* ``GET /v1/memories?namespace=...`` — 列該 namespace 全部
* ``DELETE /v1/memories/{memory_id}`` — 刪單條
* ``DELETE /v1/memories?namespace=...`` — 清整個 namespace

`namespace` 對齊 mem0 的 ``user_id`` 概念,但我們用「org_id:user_id」
作 namespace,讓同 org 不同 user 隔開。
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException
from mem0 import Memory
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mem0-sidecar")

_AUTH_SECRET = os.environ.get("MEM0_SIDECAR_AUTH", "").strip()
_QDRANT_HOST = os.environ.get("QDRANT_HOST", "qdrant")
_QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))

app = FastAPI(title="mem0 sidecar", version="1.2.0")


# ── Shared auth ──────────────────────────────────────────────────────


def _check_auth(token: Optional[str]) -> None:
    if not _AUTH_SECRET:
        raise HTTPException(503, "MEM0_SIDECAR_AUTH not configured on sidecar")
    if not token or token != _AUTH_SECRET:
        raise HTTPException(401, "invalid X-Sidecar-Auth")


# ── Request schemas ──────────────────────────────────────────────────


class LLMConfigPayload(BaseModel):
    """backend 帶過來的 LLM config — mem0 內部用它做 embedding + summarization。

    格式對齊 mem0ai 的 ``Memory.from_config`` 預期:
        {"provider": "openai"/"anthropic"/"gemini",
         "config": {"api_key": "...", "model": "..."}}
    """

    provider: str
    config: dict[str, Any]


class AddRequest(BaseModel):
    namespace: str = Field(min_length=1, description="例:org-A:user-B")
    messages: list[dict[str, Any]] = Field(
        description='list of {"role": "user"|"assistant", "content": "..."}'
    )
    llm: LLMConfigPayload
    embedder: LLMConfigPayload
    metadata: Optional[dict[str, Any]] = None


class SearchRequest(BaseModel):
    namespace: str = Field(min_length=1)
    query: str = Field(min_length=1, max_length=4000)
    llm: LLMConfigPayload
    embedder: LLMConfigPayload
    limit: int = Field(default=5, ge=1, le=20)


# ── Memory instance cache ────────────────────────────────────────────


def _make_memory_key(llm: LLMConfigPayload, embedder: LLMConfigPayload) -> str:
    """每個 (provider, model) 組合一個 Memory instance,因為內部會緩存
    向量 store 連線跟 LLM client。"""
    return (
        f"{llm.provider}:{llm.config.get('model','')}"
        f"|{embedder.provider}:{embedder.config.get('model','')}"
    )


_memory_cache: dict[str, Memory] = {}


def _get_memory(llm: LLMConfigPayload, embedder: LLMConfigPayload) -> Memory:
    """取(或建)Memory instance。Qdrant 共用 collection,namespace 區隔資料。"""
    key = _make_memory_key(llm, embedder)
    if key in _memory_cache:
        return _memory_cache[key]
    cfg = {
        "llm": {"provider": llm.provider, "config": llm.config},
        "embedder": {"provider": embedder.provider, "config": embedder.config},
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "host": _QDRANT_HOST,
                "port": _QDRANT_PORT,
                "collection_name": "rl_agent_memory",
            },
        },
        "version": "v1.1",
    }
    m = Memory.from_config(cfg)
    _memory_cache[key] = m
    log.info("created Memory instance for key=%s", key)
    return m


# ── Endpoints ────────────────────────────────────────────────────────


@app.get("/healthz")
async def healthz():
    return {"ok": True, "qdrant": f"{_QDRANT_HOST}:{_QDRANT_PORT}"}


@app.post("/v1/memories")
async def add_memory(
    payload: AddRequest,
    x_sidecar_auth: Optional[str] = Header(default=None, alias="X-Sidecar-Auth"),
):
    _check_auth(x_sidecar_auth)
    try:
        m = _get_memory(payload.llm, payload.embedder)
        result = m.add(
            messages=payload.messages,
            user_id=payload.namespace,
            metadata=payload.metadata or {},
        )
        return {"ok": True, "result": result}
    except Exception as e:  # noqa: BLE001
        log.exception("add failed")
        raise HTTPException(502, f"mem0 add failed: {type(e).__name__}: {e}") from e


@app.post("/v1/search")
async def search_memory(
    payload: SearchRequest,
    x_sidecar_auth: Optional[str] = Header(default=None, alias="X-Sidecar-Auth"),
):
    _check_auth(x_sidecar_auth)
    try:
        m = _get_memory(payload.llm, payload.embedder)
        results = m.search(
            query=payload.query,
            user_id=payload.namespace,
            limit=payload.limit,
        )
        return {"ok": True, "results": results}
    except Exception as e:  # noqa: BLE001
        log.exception("search failed")
        raise HTTPException(502, f"mem0 search failed: {type(e).__name__}: {e}") from e


@app.get("/v1/memories")
async def list_memories(
    namespace: str,
    x_sidecar_auth: Optional[str] = Header(default=None, alias="X-Sidecar-Auth"),
):
    """列該 namespace 全部 memories — UI 管理頁用。"""
    _check_auth(x_sidecar_auth)
    # list 不需要 LLM/embedder,但 mem0 Memory instance 仍要建一個拿 store handle;
    # 用 cache 內任意一個 instance(若 cache 空就要先 add 過一次)。
    if not _memory_cache:
        return {"ok": True, "results": []}
    m = next(iter(_memory_cache.values()))
    try:
        raw = m.get_all(user_id=namespace)
        # mem0 OSS 0.1.x 的 get_all 回 {"results": [...], "relations": [...]} dict;
        # 統一解包成 list 讓 caller 不必處理兩種型別
        if isinstance(raw, dict):
            results = raw.get("results") or raw.get("memories") or []
        elif isinstance(raw, list):
            results = raw
        else:
            results = []
        return {"ok": True, "results": results}
    except Exception as e:  # noqa: BLE001
        log.exception("list failed")
        raise HTTPException(502, f"mem0 list failed: {type(e).__name__}: {e}") from e


@app.delete("/v1/memories/{memory_id}")
async def delete_memory(
    memory_id: str,
    x_sidecar_auth: Optional[str] = Header(default=None, alias="X-Sidecar-Auth"),
):
    _check_auth(x_sidecar_auth)
    if not _memory_cache:
        return {"ok": True, "deleted": False}
    m = next(iter(_memory_cache.values()))
    try:
        m.delete(memory_id=memory_id)
        return {"ok": True, "deleted": True}
    except Exception as e:  # noqa: BLE001
        log.exception("delete failed")
        raise HTTPException(502, f"mem0 delete failed: {type(e).__name__}: {e}") from e


@app.delete("/v1/memories")
async def delete_all(
    namespace: str,
    x_sidecar_auth: Optional[str] = Header(default=None, alias="X-Sidecar-Auth"),
):
    """清整個 namespace — GDPR-friendly 用。"""
    _check_auth(x_sidecar_auth)
    if not _memory_cache:
        return {"ok": True, "deleted": 0}
    m = next(iter(_memory_cache.values()))
    try:
        m.delete_all(user_id=namespace)
        return {"ok": True, "namespace": namespace}
    except Exception as e:  # noqa: BLE001
        log.exception("delete_all failed")
        raise HTTPException(502, f"mem0 delete_all failed: {type(e).__name__}: {e}") from e
