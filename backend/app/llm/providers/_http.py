"""Provider 共用的 httpx 呼叫工具。

集中處理 status code → 統一錯誤類別 的轉換,避免三個 provider 各自重複。
"""
from __future__ import annotations

import httpx

from app.llm.errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
)


async def post_json(
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict,
    timeout: float,
    provider: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """POST JSON,把 HTTP error 轉成 LLMError 子類別。

    成功回 parsed JSON dict;任何失敗 raise LLMError 子類。
    ``transport`` 留給單元測試注入 ``httpx.MockTransport`` 用,正式環境留 None。
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            resp = await client.post(url, headers=headers, json=json_body)
    except httpx.TimeoutException as e:
        raise LLMTimeoutError(str(e), provider=provider) from e
    except httpx.HTTPError as e:
        raise LLMServerError(f"HTTP error: {e}", provider=provider) from e

    if resp.status_code == 401 or resp.status_code == 403:
        raise LLMAuthError(_safe_body(resp), provider=provider)
    if resp.status_code == 429:
        retry_after = resp.headers.get("retry-after")
        retry_after_sec = float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() else None
        raise LLMRateLimitError(_safe_body(resp), provider=provider, retry_after_sec=retry_after_sec)
    if 500 <= resp.status_code < 600:
        raise LLMServerError(_safe_body(resp), provider=provider, status_code=resp.status_code)
    if 400 <= resp.status_code < 500:
        raise LLMBadRequestError(_safe_body(resp), provider=provider, status_code=resp.status_code)
    return resp.json()


def _safe_body(resp: httpx.Response) -> str:
    """擷取錯誤訊息,長度截斷,避免把整包 stacktrace / HTML 寫進 log。"""
    try:
        body = resp.text
    except Exception:
        body = "<unreadable body>"
    return body[:500] if body else f"HTTP {resp.status_code}"
