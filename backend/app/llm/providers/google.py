"""Google Gemini generateContent API provider。

Reference: https://ai.google.dev/api/generate-content

關鍵差異(對比 Anthropic / OpenAI):
* URL 內含 model:``/v1beta/models/{model}:generateContent``。
* 認證走 query string ``?key=API_KEY``(也可走 OAuth bearer,我們先用 key)。
* role 命名不一樣:user 還是 "user",但助手回應是 "model"(不是 "assistant")。
* 系統提示走 top-level ``systemInstruction`` 欄位。
* 工具:``tools: [{functionDeclarations: [{name, description, parameters}]}]``。
* 工具呼叫:``candidates[0].content.parts[].functionCall = {name, args}``,
  原生沒有 tool_use_id,我們合成 ``call_{name}_{idx}`` 給上層配對用。
* 工具結果:``role="user"`` 的 ``parts[].functionResponse = {name, response}``,
  也要從合成的 id 還原回 name。
"""
from __future__ import annotations

import json
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

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GoogleProvider(LLMProvider):
    provider_name = "google"

    def __init__(
        self,
        api_key: str,
        base_url: str = _BASE,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("GoogleProvider 需要 api_key")
        self._api_key = api_key
        self._base = base_url
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
        cache_system_and_tools: bool = True,  # no-op,Google 自動 caching
        thinking_level: str | None = None,
    ) -> ChatResult:
        del cache_system_and_tools

        body: dict[str, Any] = {
            "contents": _to_google_contents(messages),
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [{"functionDeclarations": _to_google_tools(tools)}]
            body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
        # Thinking config — Gemini 2.5+ 才支援
        if is_active_level(thinking_level) and supports_thinking("google", model):
            budget = budget_tokens_for(thinking_level)
            if budget > 0:
                body["generationConfig"]["thinkingConfig"] = {
                    "thinkingBudget": budget,
                }

        url = f"{self._base}/{model}:generateContent?key={self._api_key}"
        headers = {"Content-Type": "application/json"}
        data = await post_json(
            url,
            headers=headers,
            json_body=body,
            timeout=timeout,
            provider=self.provider_name,
            transport=self._transport,
        )
        return _from_google_response(data, model=model)


def _to_google_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """Google function declaration 要求 parameters 用嚴格 OpenAPI subset。
    JSON Schema 大致相容,但 Gemini 不接受某些欄位(如 ``additionalProperties``);
    我們在這層做最小清理。"""
    return [
        {
            "name": t.name,
            "description": t.description,
            "parameters": _clean_schema_for_gemini(t.input_schema),
        }
        for t in tools
    ]


def _clean_schema_for_gemini(schema: dict[str, Any]) -> dict[str, Any]:
    """Gemini 不接受 ``additionalProperties`` / ``$schema`` / ``title``,遞迴剝除。"""
    if not isinstance(schema, dict):
        return schema
    cleaned: dict[str, Any] = {}
    for k, v in schema.items():
        if k in {"additionalProperties", "$schema", "title", "default"}:
            continue
        if isinstance(v, dict):
            cleaned[k] = _clean_schema_for_gemini(v)
        elif isinstance(v, list):
            cleaned[k] = [
                _clean_schema_for_gemini(i) if isinstance(i, dict) else i for i in v
            ]
        else:
            cleaned[k] = v
    return cleaned


def _to_google_contents(messages: list[Message]) -> list[dict[str, Any]]:
    """把統一 Message 翻成 Gemini contents 陣列。

    Gemini 沒有 tool_use_id 的概念;我們在 _from_google_response 時為每個
    tool call 合成 id =``call_{name}_{順位}``。回頭把 TOOL message 還原成
    Gemini 的 functionResponse 時,只需要 name 與 response,id 直接丟棄。
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == Role.SYSTEM:
            continue  # 應由 caller 抽到 systemInstruction
        if m.role == Role.USER:
            out.append({"role": "user", "parts": [{"text": m.content}]})
        elif m.role == Role.TOOL:
            # 從合成 id 還原 name:call_{name}_{idx} → name
            name = _name_from_synthetic_id(m.tool_call_id or "")
            out.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": name,
                                "response": _wrap_tool_response(m.content),
                            }
                        }
                    ],
                }
            )
        elif m.role == Role.ASSISTANT:
            parts: list[dict[str, Any]] = []
            if m.content:
                parts.append({"text": m.content})
            for tc in m.tool_calls:
                parts.append({"functionCall": {"name": tc.name, "args": tc.arguments}})
            out.append({"role": "model", "parts": parts})
    return out


def _wrap_tool_response(content: str) -> dict[str, Any]:
    """Gemini functionResponse.response 必須是 dict,不能是純字串。
    若內容本身已是 JSON 物件就解出來;否則包成 ``{"result": <text>}``。"""
    if not content:
        return {"result": ""}
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
        return {"result": parsed}
    except (json.JSONDecodeError, TypeError):
        return {"result": content}


def _name_from_synthetic_id(tool_call_id: str) -> str:
    """``call_{name}_{idx}`` → name。找不到 prefix 就把整個 id 當 name。"""
    if tool_call_id.startswith("call_"):
        rest = tool_call_id[len("call_") :]
        # 從右切一次 _ 去掉 idx
        if "_" in rest:
            return rest.rsplit("_", 1)[0]
        return rest
    return tool_call_id


def _from_google_response(data: dict, *, model: str) -> ChatResult:
    candidates = data.get("candidates") or []
    if not candidates:
        return _empty_result(data, model=model)
    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []

    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for idx, p in enumerate(parts):
        if "text" in p:
            text_parts.append(p["text"])
        elif "functionCall" in p:
            fc = p["functionCall"]
            name = fc.get("name", "")
            tool_calls.append(
                ToolCall(
                    id=f"call_{name}_{idx}",
                    name=name,
                    arguments=fc.get("args", {}) or {},
                )
            )

    raw_usage = data.get("usageMetadata", {}) or {}
    input_tokens = int(raw_usage.get("promptTokenCount", 0))
    output_tokens = int(raw_usage.get("candidatesTokenCount", 0))
    cached = int(raw_usage.get("cachedContentTokenCount", 0))
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
        content_text="".join(text_parts),
        tool_calls=tool_calls,
        usage=usage,
        model=model,
        provider=GoogleProvider.provider_name,
        stop_reason=_normalize_finish_reason(candidate.get("finishReason")),
        raw_response_id=data.get("responseId"),
    )


def _empty_result(data: dict, *, model: str) -> ChatResult:
    return ChatResult(
        content_text="",
        tool_calls=[],
        usage=Usage(),
        model=model,
        provider=GoogleProvider.provider_name,
        stop_reason="end_turn",
        raw_response_id=data.get("responseId"),
    )


def _normalize_finish_reason(reason: str | None) -> str:
    mapping = {
        "STOP": "end_turn",
        "MAX_TOKENS": "max_tokens",
        "SAFETY": "stop_sequence",
        "RECITATION": "stop_sequence",
        "TOOL_CALL": "tool_use",
        "OTHER": "end_turn",
    }
    return mapping.get(reason or "STOP", "end_turn")
