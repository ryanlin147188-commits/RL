"""LLM provider 統一錯誤類別。

呼叫端只要 ``except LLMError`` 就能涵蓋三家所有失敗情境;需要差異化處理
(例如 RateLimit 排到 Celery retry)再 catch 子類別。
"""
from __future__ import annotations


class LLMError(Exception):
    """所有 LLM 失敗的共同基底。``provider`` / ``status_code`` 給上層 log 用。"""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable


class LLMAuthError(LLMError):
    """401 / 403 — API key 無效或被撤銷。不應 retry,直接讓使用者重設 key。"""

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message, provider=provider, status_code=401, retryable=False)


class LLMRateLimitError(LLMError):
    """429 — 觸發速率上限。呼叫端可指數退避後 retry。"""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        retry_after_sec: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider, status_code=429, retryable=True)
        self.retry_after_sec = retry_after_sec


class LLMTimeoutError(LLMError):
    """httpx ReadTimeout / ConnectTimeout。網路問題,可 retry。"""

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message, provider=provider, status_code=None, retryable=True)


class LLMServerError(LLMError):
    """5xx — provider 端問題,可 retry。"""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status_code: int = 500,
    ) -> None:
        super().__init__(message, provider=provider, status_code=status_code, retryable=True)


class LLMBadRequestError(LLMError):
    """4xx(非 401/429)— 通常是 schema 不對或 model 不存在,不該 retry。"""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status_code: int = 400,
    ) -> None:
        super().__init__(message, provider=provider, status_code=status_code, retryable=False)
