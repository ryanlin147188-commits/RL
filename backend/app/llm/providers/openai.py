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

import logging

from app.llm.base import ChatResult, LLMProvider, Message, Role, ToolCall, ToolSpec, Usage
from app.llm.model_catalog import (
    is_active_level,
    supports_reasoning_with_tools_in_chat_completions,
    supports_thinking,
)
from app.llm.pricing import compute_cost_usd
from app.llm.providers._http import post_json

log = logging.getLogger(__name__)

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

        has_tools = bool(tools)
        wants_reasoning = (
            is_active_level(thinking_level)
            and supports_thinking("openai", model)
        )
        # gpt-5* 在 /v1/chat/completions 不允許 tools + reasoning_effort 並存,
        # 必須走 /v1/responses 新 API(2025 推出,長期會成為主要 endpoint)。
        # 當三者並存時自動切過去;其他情況走熟悉的 chat/completions。
        if (
            wants_reasoning
            and has_tools
            and not supports_reasoning_with_tools_in_chat_completions("openai", model)
        ):
            return await self._chat_via_responses_api(
                messages,
                model=model,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
                thinking_level=thinking_level,
            )

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": _to_openai_messages(messages, system=system),
        }
        if tools:
            body["tools"] = _to_openai_tools(tools)
            body["tool_choice"] = "auto"
        # 新 API 系列(o-series / gpt-5+)不接受 ``max_tokens``,必須用
        # ``max_completion_tokens``;且不允許 custom temperature(只能用預設 1.0)。
        # 用 model id 前綴判斷,不靠 thinking_level — 因為 user 即使沒開 thinking
        # 用 o-series 測連線也會觸發 OpenAI 的 "Unsupported parameter" 400 錯誤。
        m = (model or "").lower()
        needs_new_param_api = m.startswith(("o1", "o3", "o4", "o5", "gpt-5", "chatgpt-5"))
        if needs_new_param_api:
            body["max_completion_tokens"] = body.pop("max_tokens", max_tokens)
            body.pop("temperature", None)
        # Reasoning effort(走 chat/completions 路徑)
        # 注意:gpt-5* + tools + reasoning 三者並存在 chat() 開頭就已轉到
        # responses API,reach 到這的只剩:
        #   (a) o-series + reasoning(支援 + tools 並存)
        #   (b) gpt-5* + reasoning(無 tools)
        # 兩者都可以正常送 reasoning_effort。
        if wants_reasoning:
            body["reasoning_effort"] = thinking_level.lower()

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

    # ── /v1/responses API path(gpt-5* + tools + reasoning) ──────────

    async def _chat_via_responses_api(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str | None,
        tools: list[ToolSpec] | None,
        max_tokens: int,
        temperature: float,
        timeout: float,
        thinking_level: str | None,
    ) -> ChatResult:
        """OpenAI 2025 推出的 ``/v1/responses`` endpoint。schema 跟 chat/completions
        差很多:input list / instructions / flat tools / reasoning dict /
        max_output_tokens。

        為什麼要走這條路徑:gpt-5* 在 chat/completions 不允許 tools +
        reasoning_effort 並存,只能走 responses。
        """
        body: dict[str, Any] = {
            "model": model,
            "input": _to_responses_input(messages),
            "max_output_tokens": max_tokens,
        }
        if system:
            # responses API 用 top-level instructions 而非 system message
            body["instructions"] = system
        if tools:
            body["tools"] = _to_responses_tools(tools)
            body["tool_choice"] = "auto"
        if is_active_level(thinking_level) and supports_thinking("openai", model):
            body["reasoning"] = {"effort": thinking_level.lower()}
        # 注意:responses API 對 reasoning 模型也不接受 custom temperature(等同 1.0)
        # 故不送 temperature

        # URL:把 base_url 的 /chat/completions 換成 /responses
        url = self._url
        if "/chat/completions" in url:
            url = url.replace("/chat/completions", "/responses")
        elif not url.rstrip("/").endswith("/responses"):
            # base_url 形如 https://host/v1 之類 — 強制接 /responses
            url = url.rstrip("/") + "/responses"

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        data = await post_json(
            url,
            headers=headers,
            json_body=body,
            timeout=timeout,
            provider=self.provider_name,
            transport=self._transport,
        )
        return _from_responses_response(data, model=model)


def _to_responses_input(messages: list[Message]) -> list[dict[str, Any]]:
    """把統一 Message 翻成 /v1/responses 的 input list。

    格式差異:
    * user → ``{role: "user", content: [{type: "input_text", text: ...}]}``
    * assistant 文字 → ``{role: "assistant", content: [{type: "output_text", text}]}``
    * assistant tool_use → 獨立 ``{type: "function_call", call_id, name, arguments}`` items
    * tool result → ``{type: "function_call_output", call_id, output}``
    * system → 抽到 top-level instructions(caller 處理),這層跳過
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == Role.SYSTEM:
            continue
        if m.role == Role.USER:
            out.append({
                "role": "user",
                "content": [{"type": "input_text", "text": m.content or ""}],
            })
        elif m.role == Role.TOOL:
            if not m.tool_call_id:
                raise ValueError("TOOL message 必須帶 tool_call_id(responses API)")
            out.append({
                "type": "function_call_output",
                "call_id": m.tool_call_id,
                "output": m.content or "",
            })
        elif m.role == Role.ASSISTANT:
            # 純文字部分
            if m.content:
                out.append({
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": m.content}],
                })
            # tool_calls 是獨立 items
            for tc in m.tool_calls or []:
                out.append({
                    "type": "function_call",
                    "call_id": tc.id,
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments or {}, ensure_ascii=False),
                })
    return out


def _to_responses_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """/v1/responses 的 tools 格式跟 chat/completions 略不同:扁平的,
    不包 function wrapper。"""
    return [
        {
            "type": "function",
            "name": t.name,
            "description": t.description,
            "parameters": t.input_schema,
        }
        for t in tools
    ]


def _from_responses_response(data: dict, *, model: str) -> ChatResult:
    """/v1/responses 回應 → 統一 ChatResult。

    output 是 list of items:
    * ``{type: "message", role: "assistant", content: [{type: "output_text", text}]}``
    * ``{type: "function_call", call_id, name, arguments}``
    * ``{type: "reasoning", ...}`` — thinking 過程,目前不直接暴露給 chat content
    """
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for item in data.get("output", []) or []:
        kind = item.get("type")
        if kind == "message":
            for c in item.get("content", []) or []:
                if c.get("type") == "output_text":
                    text_parts.append(c.get("text", ""))
        elif kind == "function_call":
            raw_args = item.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                args = {"__raw__": raw_args}
            tool_calls.append(ToolCall(
                id=item.get("call_id", ""),
                name=item.get("name", ""),
                arguments=args,
            ))
        elif kind == "reasoning":
            # thinking 過程不暴露給 LLM 對話內容(避免 inject 思考細節)
            pass

    raw_usage = data.get("usage") or {}
    input_tokens = int(raw_usage.get("input_tokens", 0))
    output_tokens = int(raw_usage.get("output_tokens", 0))
    cached = int((raw_usage.get("input_tokens_details") or {}).get("cached_tokens", 0))
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

    # stop_reason normalize
    if tool_calls:
        stop_reason = "tool_use"
    elif (data.get("status") or "").lower() == "incomplete":
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"

    return ChatResult(
        content_text="".join(text_parts),
        tool_calls=tool_calls,
        usage=usage,
        model=data.get("model", model),
        provider=OpenAIProvider.provider_name,
        stop_reason=stop_reason,
        raw_response_id=data.get("id"),
    )


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
