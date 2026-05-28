"""OpenAI Chat Completions API provider。

Reference: https://platform.openai.com/docs/api-reference/chat

關鍵差異(對比 Anthropic):
* system 是 messages 裡的一條,role="system"(我們在這層自動插到最前)。
* tools 格式:``[{"type": "function", "function": {name, description, parameters}}]``。
* tool_calls 在 ``choices[0].message.tool_calls``,arguments 是 **JSON 字串**
  不是 dict,需要 ``json.loads`` 解析。
* tool 結果走獨立的 role="tool" message,帶 ``tool_call_id``。
* Prompt caching 是自動的(prefix > 1024 tokens 才生效),沒有顯式控制 —
  ``cache_system_and_tools`` 參數在這裡 no-op。``usage.prompt_tokens_details
  .cached_tokens`` 會回 cache hit 量,我們填進 ``cache_read_tokens``。
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from app.llm.base import ChatResult, LLMProvider, Message, Role, ToolCall, ToolSpec, Usage
from app.llm.model_catalog import is_active_level, supports_thinking
from app.llm.pricing import compute_cost_usd
from app.llm.providers._http import post_json

_ENDPOINT = "https://api.openai.com/v1/chat/completions"


class OpenAIProvider(LLMProvider):
    provider_name = "openai"

    def __init__(
        self,
        api_key: str,
        base_url: str = _ENDPOINT,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """``base_url`` 可覆寫,支援 OpenAI-compatible 本地推論伺服器
        (vLLM / Ollama / LM Studio)— 跟既有 requirements.txt 註解一致。
        ``transport`` 給單元測試注入 MockTransport 用。"""
        if not api_key:
            raise ValueError("OpenAIProvider 需要 api_key")
        self._api_key = api_key
        self._url = base_url
        self._transport = transport

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
        cache_system_and_tools: bool = True,  # no-op,OpenAI 自動 caching
        thinking_level: str | None = None,
    ) -> ChatResult:
        del cache_system_and_tools  # 顯式忽略,避免 linter 抱怨

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": _to_openai_messages(messages, system=system),
        }
        if tools:
            body["tools"] = _to_openai_tools(tools)
            body["tool_choice"] = "auto"
        # Reasoning effort — 只在 o-series / 支援的 model + level 有效時送
        if is_active_level(thinking_level) and supports_thinking("openai", model):
            body["reasoning_effort"] = thinking_level.lower()
            # o-series 不接受 temperature,改成不送(provider 預設 1.0)
            body.pop("temperature", None)
            # o-series 用 max_completion_tokens 而非 max_tokens(新 API)
            body["max_completion_tokens"] = body.pop("max_tokens", max_tokens)

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        data = await post_json(
            self._url,
            headers=headers,
            json_body=body,
            timeout=timeout,
            provider=self.provider_name,
            transport=self._transport,
        )
        return _from_openai_response(data, model=model)


def _to_openai_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def _to_openai_messages(messages: list[Message], *, system: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        if m.role == Role.SYSTEM:
            # 多個 system 訊息合併;先 append text
            out.append({"role": "system", "content": m.content})
        elif m.role == Role.USER:
            out.append({"role": "user", "content": m.content})
        elif m.role == Role.TOOL:
            if not m.tool_call_id:
                raise ValueError("TOOL message 必須帶 tool_call_id")
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m.tool_call_id,
                    "content": m.content,
                }
            )
        elif m.role == Role.ASSISTANT:
            msg: dict[str, Any] = {"role": "assistant", "content": m.content or None}
            if m.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in m.tool_calls
                ]
            out.append(msg)
    return out


def _from_openai_response(data: dict, *, model: str) -> ChatResult:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message", {}) or {}
    content_text = msg.get("content") or ""

    tool_calls: list[ToolCall] = []
    for tc in msg.get("tool_calls") or []:
        if tc.get("type") != "function":
            continue
        fn = tc.get("function", {}) or {}
        raw_args = fn.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            # LLM 偶爾會回 malformed JSON;保留原字串供 debug,呼叫端應驗證
            args = {"__raw__": raw_args}
        tool_calls.append(
            ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args)
        )

    raw_usage = data.get("usage", {}) or {}
    input_tokens = int(raw_usage.get("prompt_tokens", 0))
    output_tokens = int(raw_usage.get("completion_tokens", 0))
    cached = int((raw_usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
    # OpenAI 把 cached 含在 prompt_tokens 內,為了讓 pricing 不重複計,把
    # cached 部分從 input 扣掉(雖然 OpenAI 自動 cache 沒有額外計費差異,
    # 但 cost 計算保持「fresh input」一致語意)。
    fresh_input = max(input_tokens - cached, 0)
    usage = Usage(
        input_tokens=fresh_input,
        output_tokens=output_tokens,
        cache_read_tokens=cached,
        cache_write_tokens=0,
        cost_usd=compute_cost_usd(
            model,
            input_tokens=fresh_input,
            output_tokens=output_tokens,
            cache_read_tokens=cached,
        ),
    )

    return ChatResult(
        content_text=content_text,
        tool_calls=tool_calls,
        usage=usage,
        model=data.get("model", model),
        provider=OpenAIProvider.provider_name,
        stop_reason=_normalize_finish_reason(choice.get("finish_reason")),
        raw_response_id=data.get("id"),
    )


def _normalize_finish_reason(reason: str | None) -> str:
    """OpenAI finish_reason → Anthropic-style stop_reason。"""
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "stop_sequence",
        "function_call": "tool_use",  # 舊版相容
    }
    return mapping.get(reason or "stop", "end_turn")
