"""mem0 sidecar HTTP client — backend 端的 thin wrapper。

設計原則:
* **Fail-open**:sidecar 連不上 / 401 / 5xx 都不擋 chat;只 log warning。
  Memory 是「錦上添花」,沒它使用者仍能正常對話。
* **每次帶 LLM config**:不在 sidecar 預存 OpenAI key,backend 把該 org 的
  LlmProviderConfig 解密後 per-request 帶上去。sidecar 內部 cache。
* **Namespace = org_id:user_id**:per-user 隔離記憶,但仍標明 org 邊界
  方便未來 org-level 共用設計。
* **Sanitize**:從 mem0 recall 回的字串走 wrap_user_data XML 包裝後再塞
  進 system prompt(防 prompt injection — 過去存進去的記憶內容可能含
  「ignore previous instructions」字眼)。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.agent.sanitize import wrap_user_data
from app.config import settings

log = logging.getLogger(__name__)


def _make_namespace(*, organization_id: Optional[str], user_id: str) -> str:
    """org_id:user_id;org_id 為 None(superuser)時用 _global。"""
    return f"{organization_id or '_global'}:{user_id}"


def _llm_config_for_mem0(
    *, provider: str, api_key: str, model: Optional[str] = None
) -> dict[str, Any]:
    """把 RL 的 LlmProviderConfig 轉成 mem0 sidecar 預期的格式。

    mem0 內部走自家 SDK(openai / anthropic / gemini);model 不指定就用
    mem0 預設(通常是該家的 cheap model)。
    """
    cfg: dict[str, Any] = {"api_key": api_key}
    if model:
        cfg["model"] = model
    return {"provider": provider, "config": cfg}


def _embedder_config(
    *, provider: str, api_key: str
) -> dict[str, Any]:
    """embedding model 每家有自家規格;不在 LlmProviderConfig 設,寫死預設。"""
    if provider == "openai":
        return {"provider": "openai", "config": {"api_key": api_key, "model": "text-embedding-3-small"}}
    if provider == "anthropic":
        # Anthropic 沒自家 embedding API,fallback 用 OpenAI 但需要 user 另設;
        # 沒設就 raise(caller fail-open 自會吃)
        raise ValueError("anthropic 沒有自家 embedding model;請至少設一把 OpenAI key 給 mem0 用")
    if provider == "google":
        return {"provider": "gemini", "config": {"api_key": api_key, "model": "models/text-embedding-004"}}
    raise ValueError(f"unknown embedder provider: {provider}")


async def _post(
    path: str,
    body: dict[str, Any],
    *,
    timeout: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """POST 到 sidecar;失敗回 None。"""
    url = settings.MEM0_SIDECAR_URL.rstrip("/") + path
    auth = settings.MEM0_SIDECAR_AUTH
    if not auth:
        log.debug("mem0 disabled (MEM0_SIDECAR_AUTH empty)")
        return None
    headers = {"X-Sidecar-Auth": auth, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=timeout or settings.MEM0_TIMEOUT_SEC) as client:
            resp = await client.post(url, headers=headers, json=body)
        if resp.status_code >= 400:
            log.warning("mem0 %s %s: %s", path, resp.status_code, resp.text[:300])
            return None
        return resp.json()
    except httpx.HTTPError as e:
        log.warning("mem0 %s connect failed: %s", path, e)
        return None


async def _get(
    path: str, params: dict[str, str], *, timeout: Optional[float] = None
) -> Optional[dict[str, Any]]:
    url = settings.MEM0_SIDECAR_URL.rstrip("/") + path
    auth = settings.MEM0_SIDECAR_AUTH
    if not auth:
        return None
    headers = {"X-Sidecar-Auth": auth}
    try:
        async with httpx.AsyncClient(timeout=timeout or settings.MEM0_TIMEOUT_SEC) as client:
            resp = await client.get(url, headers=headers, params=params)
        if resp.status_code >= 400:
            log.warning("mem0 GET %s %s: %s", path, resp.status_code, resp.text[:300])
            return None
        return resp.json()
    except httpx.HTTPError as e:
        log.warning("mem0 GET %s connect failed: %s", path, e)
        return None


async def _delete(path: str, params: Optional[dict[str, str]] = None) -> Optional[dict[str, Any]]:
    url = settings.MEM0_SIDECAR_URL.rstrip("/") + path
    auth = settings.MEM0_SIDECAR_AUTH
    if not auth:
        return None
    headers = {"X-Sidecar-Auth": auth}
    try:
        async with httpx.AsyncClient(timeout=settings.MEM0_TIMEOUT_SEC) as client:
            resp = await client.delete(url, headers=headers, params=params or {})
        if resp.status_code >= 400:
            log.warning("mem0 DELETE %s %s: %s", path, resp.status_code, resp.text[:300])
            return None
        return resp.json()
    except httpx.HTTPError as e:
        log.warning("mem0 DELETE %s connect failed: %s", path, e)
        return None


def is_enabled() -> bool:
    """sidecar 有設 auth secret 才算啟用。"""
    return bool(settings.MEM0_SIDECAR_AUTH)


# ── High-level API:給 agent_service 用 ──────────────────────────────


async def recall_for_prompt(
    *,
    organization_id: Optional[str],
    user_id: str,
    query: str,
    llm_provider: str,
    llm_api_key: str,
    llm_model: Optional[str] = None,
) -> Optional[str]:
    """語意搜尋過去記憶,回一段「準備好塞進 system prompt」的文字。

    回 None = 沒啟用 mem0 / 沒記憶 / sidecar 不通。caller 拿到 None 就不動
    system prompt。

    回的字串已用 ``wrap_user_data`` XML 包裝,可直接 concat 進 system。
    """
    if not is_enabled():
        return None
    try:
        embedder = _embedder_config(provider=llm_provider, api_key=llm_api_key)
    except ValueError as e:
        log.info("mem0 recall skipped: %s", e)
        return None
    body = {
        "namespace": _make_namespace(organization_id=organization_id, user_id=user_id),
        "query": query,
        "llm": _llm_config_for_mem0(provider=llm_provider, api_key=llm_api_key, model=llm_model),
        "embedder": embedder,
        "limit": settings.MEM0_RECALL_LIMIT,
    }
    data = await _post("/v1/search", body)
    if not data or not data.get("ok"):
        return None
    results = data.get("results") or []
    # mem0 search 結果結構:[{memory: "...", score: 0.x, ...}, ...]
    items = []
    for r in results[: settings.MEM0_RECALL_LIMIT]:
        text = r.get("memory") if isinstance(r, dict) else str(r)
        if text:
            items.append(text)
    if not items:
        return None
    # 包進 XML,LLM 看到知道是「過去記憶」資料而非新指令
    joined = "\n".join(f"- {t}" for t in items)
    return wrap_user_data(joined, field_name="user_memories", max_len=4000)


async def add_messages(
    *,
    organization_id: Optional[str],
    user_id: str,
    messages: list[dict[str, str]],
    llm_provider: str,
    llm_api_key: str,
    llm_model: Optional[str] = None,
) -> bool:
    """把對話歷史交給 mem0 — sidecar 內部用 LLM 摘錄出值得記的 facts。

    ``messages`` 格式 [{"role": "user"/"assistant", "content": "..."}, ...]。
    回 True 表 sidecar 接受;False = 失敗 / 沒啟用。
    """
    if not is_enabled() or not messages:
        return False
    try:
        embedder = _embedder_config(provider=llm_provider, api_key=llm_api_key)
    except ValueError:
        return False
    body = {
        "namespace": _make_namespace(organization_id=organization_id, user_id=user_id),
        "messages": messages,
        "llm": _llm_config_for_mem0(provider=llm_provider, api_key=llm_api_key, model=llm_model),
        "embedder": embedder,
    }
    data = await _post("/v1/memories", body, timeout=settings.MEM0_TIMEOUT_SEC * 2)
    return bool(data and data.get("ok"))


async def list_memories(
    *, organization_id: Optional[str], user_id: str
) -> list[dict[str, Any]]:
    """列該 user 全部 memories — UI 管理頁用。fail → 空 list。"""
    if not is_enabled():
        return []
    ns = _make_namespace(organization_id=organization_id, user_id=user_id)
    data = await _get("/v1/memories", {"namespace": ns})
    if not data or not data.get("ok"):
        return []
    return data.get("results") or []


async def delete_memory(*, memory_id: str) -> bool:
    if not is_enabled():
        return False
    data = await _delete(f"/v1/memories/{memory_id}")
    return bool(data and data.get("ok"))


async def delete_all_for_user(
    *, organization_id: Optional[str], user_id: str
) -> bool:
    if not is_enabled():
        return False
    ns = _make_namespace(organization_id=organization_id, user_id=user_id)
    data = await _delete("/v1/memories", {"namespace": ns})
    return bool(data and data.get("ok"))
