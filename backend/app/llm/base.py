"""LLM 抽象介面與資料模型。

統一三家供應商的 request / response 格式。各 provider 子類別負責把
這層的內部表示翻譯成自家 API,以及把回應翻回來。

訊息流向(以 tool use 為例):
    user -> Message(role=USER, content="跑一下 case 42")
    -> LLMProvider.chat() 內部翻成各家格式 -> HTTP
    -> ChatResult(tool_calls=[ToolCall(name="run_test_case", args={"case_id": 42})])
    呼叫端(agent/executor)派發到 Celery,結果回填
    -> Message(role=TOOL, content="...", tool_call_id="...")
    -> LLMProvider.chat() 再轉一次,LLM 產出最終文字回應

關鍵設計決定:
* ``Message.content`` 對 USER / ASSISTANT / SYSTEM 是純文字;對 TOOL 是
  「tool 執行結果文字」。多模態(image)留給 Phase 3。
* ``ToolSpec.input_schema`` 用 JSON Schema(三家都認),內部不再做進一步抽象。
* ``Usage.cached_*_tokens`` 只有 Anthropic 會填(其他家自動 caching 不透明)。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class ToolSpec:
    """工具的對外契約 — 餵給 LLM 看的部分。

    ``requires_confirmation`` 是 RL 專案的擴充欄位,不會送進 LLM 的 schema,
    而是給 agent/executor 在實際 dispatch 前判斷要不要插入「人類二次確認」。
    對應紅線之一:destructive action(刪資料 / 跑生產環境)必須 confirm。
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    requires_confirmation: bool = False


@dataclass(frozen=True)
class ToolCall:
    """LLM 回傳的工具呼叫請求。

    ``id`` 是 provider 給的 tool_use_id,後續 TOOL message 必須帶這個 id 配對。
    Google Gemini 原生沒有 tool_use_id,我們以 ``f"call_{name}_{idx}"`` 模擬。
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None


@dataclass
class Usage:
    """單次回應的 token 用量與成本。

    ``cost_usd`` 由 pricing.py 在 provider 內補上;cached tokens 只有 Anthropic
    顯式回傳,其他家補 0。``cache_read_tokens`` 命中越多越省錢。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class ChatResult:
    """單次 chat 呼叫的完整結果。

    ``content_text`` 與 ``tool_calls`` 可能同時存在(LLM 一邊講一邊呼叫工具)。
    呼叫端應該:有 tool_calls 就先派發、把結果回填、再次呼叫 chat();沒有
    tool_calls 就把 ``content_text`` 顯示給使用者。

    ``stop_reason`` 對齊 Anthropic 命名("end_turn" / "tool_use" / "max_tokens"
    / "stop_sequence"),OpenAI / Google 在 provider 內 normalize。
    """

    content_text: str
    tool_calls: list[ToolCall]
    usage: Usage
    model: str
    provider: str
    stop_reason: str
    raw_response_id: str | None = None


class LLMProvider(ABC):
    """單一 LLM 供應商 adapter 的抽象基底。

    每個 provider 實例對應一家(Anthropic / OpenAI / Google)。router 依照
    model_id 前綴選對應實例;同一 provider 可服務多個 model(例如 OpenAI
    的 gpt-4o-mini 與 gpt-4o 共用一個 OpenAIProvider 實例)。

    子類別只需實作 ``chat()``。Stream 介面留給 Phase 1(聊天框打字機效果)。
    """

    provider_name: str = ""  # subclass override

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        timeout: float = 60.0,
        cache_system_and_tools: bool = True,
    ) -> ChatResult:
        """同步呼叫一次 LLM,回完整 ChatResult。

        Args:
            messages: 對話歷史(不含 system)。最後一則通常是 USER 或 TOOL。
            model: 完整 model id(claude-opus-4-7 / gpt-4o / gemini-2.5-pro)。
            system: 系統提示。Anthropic / Google 走獨立欄位,OpenAI 會插成
                首條 message。
            tools: 可用工具列表;傳 None 表示這輪不允許工具。
            max_tokens: 回應的 token 上限。
            temperature: 0.0(穩定)~ 1.0(發散)。
            timeout: HTTP 整體 timeout(秒)。長 tool 不在這裡 block。
            cache_system_and_tools: 是否要對 system + tools 加 prompt cache
                breakpoint。只有 Anthropic 會有實際效果,其他家忽略。

        Raises:
            LLMAuthError: 401 / 403。
            LLMRateLimitError: 429。
            LLMTimeoutError: 連線或讀取超時。
            LLMServerError: 5xx。
            LLMBadRequestError: 其他 4xx。
        """
        raise NotImplementedError
