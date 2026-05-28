"""Anthropic Messages API provider。

Reference: https://docs.anthropic.com/en/api/messages

關鍵設計:
* Prompt caching:當 ``cache_system_and_tools=True`` 時,system block 與
  tools 陣列尾端各放一個 ``cache_control: {"type": "ephemeral"}``,讓
  Anthropic 把這兩塊 cache 起來,後續同 session 重用可省 90% input cost。
  RL 的 agent 系統提示 + 5 個 tools 加起來大約 2~4k tokens,值得 cache。
* tool_use / tool_result block:Anthropic 把 tool call 包在 assistant
  content 的 ``tool_use`` block,工具結果則是 user content 裡的
  ``tool_result`` block。需要在 Message 轉換時還原。
"""
from __future__ import annotations

from typing import Any

import httpx

from app.llm.base import ChatResult, LLMProvider, Message, Role, ToolCall, ToolSpec, Usage
from app.llm.model_catalog import (
    budget_tokens_for,
    is_active_level,
    supports_thinking,
)
from app.llm.pricing import compute_cost_usd
from app.llm.providers._http import post_json

_ENDPOINT = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"


class AnthropicProvider(LLMProvider):
    provider_name = "anthropic"

    def __init__(
        self,
        api_key: str,
        base_url: str = _ENDPOINT,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("AnthropicProvider 需要 api_key")
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
        cache_system_and_tools: bool = True,
        thinking_level: str | None = None,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": _to_anthropic_messages(messages),
        }
        if system:
            body["system"] = _system_blocks(system, cache=cache_system_and_tools)
        if tools:
            body["tools"] = _to_anthropic_tools(tools, cache=cache_system_and_tools)
        # Extended thinking — 只在 model 支援 + level 有效時送
        if is_active_level(thinking_level) and supports_thinking("anthropic", model):
            budget = budget_tokens_for(thinking_level)
            if budget > 0:
                # Anthropic 要求 budget_tokens < max_tokens;不夠的話自動拉高 max_tokens
                if budget >= body["max_tokens"]:
                    body["max_tokens"] = budget + 1024
                body["thinking"] = {"type": "enabled", "budget_tokens": budget}
                # extended thinking 不接受 temperature != 1
                body["temperature"] = 1.0

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }
        data = await post_json(
            self._url,
            headers=headers,
            json_body=body,
            timeout=timeout,
            provider=self.provider_name,
            transport=self._transport,
        )
        return _from_anthropic_response(data, model=model)


def _system_blocks(system: str, *, cache: bool) -> list[dict[str, Any]]:
    """Anthropic 的 system 可以是 string 或 content block 陣列;
    為了能加 cache_control,我們一律用 block 陣列形式。"""
    block: dict[str, Any] = {"type": "text", "text": system}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


def _to_anthropic_tools(tools: list[ToolSpec], *, cache: bool) -> list[dict[str, Any]]:
    """tools 陣列的最後一個元素加 cache_control,Anthropic 會把整段 tools
    block 一起 cache(只要結尾有 breakpoint)。"""
    out: list[dict[str, Any]] = []
    for i, t in enumerate(tools):
        item: dict[str, Any] = {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        if cache and i == len(tools) - 1:
            item["cache_control"] = {"type": "ephemeral"}
        out.append(item)
    return out


def _to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """把統一 Message 翻成 Anthropic messages 格式。

    規則:
    * USER → role=user, content=[{type: "text", text}](單純文字)
    * TOOL → role=user, content=[{type: "tool_result", tool_use_id, content}]
      (Anthropic 把工具結果歸在 user role)
    * ASSISTANT 文字 → role=assistant, content=[{type: "text", text}]
    * ASSISTANT 有 tool_calls → role=assistant, content 同時含 text + tool_use blocks
    * SYSTEM 不能出現在 messages(Anthropic 的 system 是 top-level 欄位)
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == Role.SYSTEM:
            # Defensive — caller 應該把 system 從 messages 抽出來
            continue
        if m.role == Role.USER:
            out.append({"role": "user", "content": [{"type": "text", "text": m.content}]})
        elif m.role == Role.TOOL:
            if not m.tool_call_id:
                raise ValueError("TOOL message 必須帶 tool_call_id")
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.tool_call_id,
                            "content": m.content,
                        }
                    ],
                }
            )
        elif m.role == Role.ASSISTANT:
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    }
                )
            out.append({"role": "assistant", "content": blocks})
    return out


def _from_anthropic_response(data: dict, *, model: str) -> ChatResult:
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=block.get("input", {}) or {},
                )
            )

    raw_usage = data.get("usage", {}) or {}
    input_tokens = int(raw_usage.get("input_tokens", 0))
    output_tokens = int(raw_usage.get("output_tokens", 0))
    cache_read = int(raw_usage.get("cache_read_input_tokens", 0))
    cache_write = int(raw_usage.get("cache_creation_input_tokens", 0))
    usage = Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=compute_cost_usd(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        ),
    )

    return ChatResult(
        content_text="".join(text_parts),
        tool_calls=tool_calls,
        usage=usage,
        model=data.get("model", model),
        provider=AnthropicProvider.provider_name,
        stop_reason=data.get("stop_reason", "end_turn"),
        raw_response_id=data.get("id"),
    )
