"""LLM abstraction layer — 統一封裝三家供應商(Anthropic / OpenAI / Google)。

設計目標:
1. 對外只暴露 ``llm_router.chat(...)``,呼叫端不需要知道用的是哪家。
2. 不引入新的供應商 SDK(沿用既有 httpx==0.27.2,降低供應鏈攻擊面)。
3. 以 Anthropic prompt caching 作為「最強假設」設計;OpenAI / Google
   沒有顯式 cache control 就 graceful no-op。
4. 所有長時間 tool 一律由呼叫端走 Celery + Valkey 非同步,本模組不
   block(LLM HTTP 預設 60s timeout)。

不在這層處理的事:
* 工具註冊與執行(屬 agent/ 模組,Phase 1)
* 對話 session 與訊息歷史儲存(屬 hermes/agent_sessions,Phase 1)
* API key 入 DB + Fernet 加密(屬 llm_provider_configs,Phase 0 後半段)
"""
from app.llm.base import (
    ChatResult,
    LLMProvider,
    Message,
    Role,
    ToolCall,
    ToolSpec,
    Usage,
)
from app.llm.errors import (
    LLMAuthError,
    LLMError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
)
from app.llm.router import get_provider, get_provider_for_chat, infer_provider

__all__ = [
    "ChatResult",
    "LLMAuthError",
    "LLMError",
    "LLMProvider",
    "LLMRateLimitError",
    "LLMServerError",
    "LLMTimeoutError",
    "Message",
    "Role",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "get_provider",
    "get_provider_for_chat",
    "infer_provider",
]
