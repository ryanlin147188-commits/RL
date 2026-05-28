"""LLM provider 單元測試。

沿用 [[test_hermes_client]] 的 httpx.MockTransport 模式,不打真實 API,只驗
三件事:
1. 統一 Message/ToolSpec → 各家 request payload 的轉換正確
2. 各家 response → 統一 ChatResult 的解析正確(含 tool_calls / usage)
3. HTTP status code → LLMError 子類別映射(401/429/5xx)
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.llm import (
    LLMAuthError,
    LLMRateLimitError,
    LLMServerError,
    Message,
    Role,
    ToolCall,
    ToolSpec,
    infer_provider,
)
from app.llm.errors import LLMBadRequestError
from app.llm.providers import AnthropicProvider, GoogleProvider, OpenAIProvider


# ── helpers ─────────────────────────────────────────────────────────


def _mock(payload: dict, status: int = 200, headers: dict | None = None):
    """回一個 MockTransport,把 request 也存下來供 assert。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(status, json=payload, headers=headers or {})

    return httpx.MockTransport(handler), captured


_SAMPLE_TOOL = ToolSpec(
    name="run_test_case",
    description="Run a RL testcase by id and return execution_id.",
    input_schema={
        "type": "object",
        "properties": {"case_id": {"type": "integer"}},
        "required": ["case_id"],
        "additionalProperties": False,
    },
)


# ── Anthropic ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_request_includes_cache_control_on_system_and_last_tool() -> None:
    transport, captured = _mock(
        {
            "id": "msg_01",
            "model": "claude-opus-4-7",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
    )
    p = AnthropicProvider(api_key="sk-test", transport=transport)
    await p.chat(
        [Message(Role.USER, "hello")],
        model="claude-opus-4-7",
        system="you are a QA agent",
        tools=[_SAMPLE_TOOL],
    )

    body = captured["body"]
    assert isinstance(body["system"], list)
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert captured["headers"]["x-api-key"] == "sk-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
async def test_anthropic_parses_tool_use_and_cache_usage() -> None:
    transport, _ = _mock(
        {
            "id": "msg_02",
            "model": "claude-opus-4-7",
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "ok let me run it"},
                {
                    "type": "tool_use",
                    "id": "toolu_abc",
                    "name": "run_test_case",
                    "input": {"case_id": 42},
                },
            ],
            "usage": {
                "input_tokens": 5,
                "output_tokens": 8,
                "cache_creation_input_tokens": 1500,
                "cache_read_input_tokens": 2000,
            },
        }
    )
    p = AnthropicProvider(api_key="sk-test", transport=transport)
    result = await p.chat([Message(Role.USER, "go")], model="claude-opus-4-7")

    assert result.content_text == "ok let me run it"
    assert result.tool_calls == [
        ToolCall(id="toolu_abc", name="run_test_case", arguments={"case_id": 42})
    ]
    assert result.usage.cache_read_tokens == 2000
    assert result.usage.cache_write_tokens == 1500
    assert result.usage.cost_usd > 0
    assert result.stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_anthropic_tool_result_message_round_trip() -> None:
    """ASSISTANT(tool_use) + TOOL(tool_result) 對話歷史能正確構造 request。"""
    transport, captured = _mock(
        {
            "id": "msg_03",
            "model": "claude-opus-4-7",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "done"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )
    p = AnthropicProvider(api_key="sk-test", transport=transport)
    history = [
        Message(Role.USER, "run 42"),
        Message(
            Role.ASSISTANT,
            "",
            tool_calls=[ToolCall(id="toolu_x", name="run_test_case", arguments={"case_id": 42})],
        ),
        Message(Role.TOOL, '{"execution_id": 99}', tool_call_id="toolu_x"),
    ]
    await p.chat(history, model="claude-opus-4-7")

    msgs = captured["body"]["messages"]
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"][0]["type"] == "tool_use"
    assert msgs[2]["role"] == "user"
    assert msgs[2]["content"][0]["type"] == "tool_result"
    assert msgs[2]["content"][0]["tool_use_id"] == "toolu_x"


# ── OpenAI ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_inserts_system_message_at_head_and_tools() -> None:
    transport, captured = _mock(
        {
            "id": "chatcmpl-1",
            "model": "gpt-4o-mini",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }
    )
    p = OpenAIProvider(api_key="sk-test", transport=transport)
    await p.chat(
        [Message(Role.USER, "hello")],
        model="gpt-4o-mini",
        system="you are a QA agent",
        tools=[_SAMPLE_TOOL],
    )

    body = captured["body"]
    assert body["messages"][0] == {"role": "system", "content": "you are a QA agent"}
    assert body["messages"][1] == {"role": "user", "content": "hello"}
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "run_test_case"
    assert body["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_openai_parses_tool_calls_with_json_string_arguments() -> None:
    transport, _ = _mock(
        {
            "id": "chatcmpl-2",
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "run_test_case",
                                    "arguments": '{"case_id": 42}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 80},
            },
        }
    )
    p = OpenAIProvider(api_key="sk-test", transport=transport)
    result = await p.chat([Message(Role.USER, "go")], model="gpt-4o-mini")

    assert result.tool_calls == [
        ToolCall(id="call_abc", name="run_test_case", arguments={"case_id": 42})
    ]
    assert result.stop_reason == "tool_use"
    # cached 從 prompt_tokens 扣除,留 20 作 fresh
    assert result.usage.input_tokens == 20
    assert result.usage.cache_read_tokens == 80


@pytest.mark.asyncio
async def test_openai_serializes_assistant_tool_call_arguments_back_to_string() -> None:
    transport, captured = _mock(
        {
            "id": "x",
            "model": "gpt-4o-mini",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )
    p = OpenAIProvider(api_key="sk-test", transport=transport)
    history = [
        Message(Role.USER, "run 42"),
        Message(
            Role.ASSISTANT,
            "",
            tool_calls=[ToolCall(id="call_x", name="run_test_case", arguments={"case_id": 42})],
        ),
        Message(Role.TOOL, '{"execution_id": 99}', tool_call_id="call_x"),
    ]
    await p.chat(history, model="gpt-4o-mini")

    msgs = captured["body"]["messages"]
    assistant_msg = next(m for m in msgs if m["role"] == "assistant")
    assert assistant_msg["tool_calls"][0]["function"]["arguments"] == '{"case_id": 42}'
    tool_msg = next(m for m in msgs if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "call_x"


# ── Google ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_google_request_uses_systemInstruction_and_strips_additionalProperties() -> None:
    transport, captured = _mock(
        {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "hi"}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2},
        }
    )
    p = GoogleProvider(api_key="key-test", transport=transport)
    await p.chat(
        [Message(Role.USER, "hello")],
        model="gemini-2.5-flash",
        system="you are a QA agent",
        tools=[_SAMPLE_TOOL],
    )

    assert "key=key-test" in captured["url"]
    assert "gemini-2.5-flash:generateContent" in captured["url"]
    body = captured["body"]
    assert body["systemInstruction"]["parts"][0]["text"] == "you are a QA agent"
    fn_decl = body["tools"][0]["functionDeclarations"][0]
    assert fn_decl["name"] == "run_test_case"
    # additionalProperties 應該被剝掉(Gemini 不接受)
    assert "additionalProperties" not in fn_decl["parameters"]
    assert body["contents"][0] == {"role": "user", "parts": [{"text": "hello"}]}


@pytest.mark.asyncio
async def test_google_parses_function_call_with_synthetic_id() -> None:
    transport, _ = _mock(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"text": "running"},
                            {
                                "functionCall": {
                                    "name": "run_test_case",
                                    "args": {"case_id": 42},
                                }
                            },
                        ],
                    },
                    "finishReason": "TOOL_CALL",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 50,
                "candidatesTokenCount": 5,
                "cachedContentTokenCount": 30,
            },
        }
    )
    p = GoogleProvider(api_key="key-test", transport=transport)
    result = await p.chat([Message(Role.USER, "go")], model="gemini-2.5-flash")

    assert result.content_text == "running"
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.name == "run_test_case"
    assert tc.arguments == {"case_id": 42}
    # 合成 id 格式:call_{name}_{idx}
    assert tc.id.startswith("call_run_test_case_")
    assert result.stop_reason == "tool_use"
    assert result.usage.cache_read_tokens == 30
    assert result.usage.input_tokens == 20  # 50 - 30 cached


@pytest.mark.asyncio
async def test_google_tool_response_round_trip_recovers_name_from_synthetic_id() -> None:
    transport, captured = _mock(
        {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "done"}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
        }
    )
    p = GoogleProvider(api_key="key-test", transport=transport)
    history = [
        Message(Role.USER, "run 42"),
        Message(
            Role.ASSISTANT,
            "",
            tool_calls=[
                ToolCall(
                    id="call_run_test_case_1",
                    name="run_test_case",
                    arguments={"case_id": 42},
                )
            ],
        ),
        Message(
            Role.TOOL,
            '{"execution_id": 99}',
            tool_call_id="call_run_test_case_1",
        ),
    ]
    await p.chat(history, model="gemini-2.5-flash")

    contents = captured["body"]["contents"]
    # 最後一條應該是 functionResponse,name 還原成 "run_test_case"
    last = contents[-1]
    assert last["role"] == "user"
    fr = last["parts"][0]["functionResponse"]
    assert fr["name"] == "run_test_case"
    # response 必須包成 dict
    assert fr["response"] == {"execution_id": 99}


# ── Error mapping ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_401_raises_auth_error() -> None:
    transport, _ = _mock({"error": "invalid key"}, status=401)
    p = AnthropicProvider(api_key="sk-bad", transport=transport)
    with pytest.raises(LLMAuthError) as ei:
        await p.chat([Message(Role.USER, "hi")], model="claude-opus-4-7")
    assert ei.value.provider == "anthropic"


@pytest.mark.asyncio
async def test_http_429_raises_rate_limit_with_retry_after() -> None:
    transport, _ = _mock({"error": "rate"}, status=429, headers={"retry-after": "12"})
    p = OpenAIProvider(api_key="sk-test", transport=transport)
    with pytest.raises(LLMRateLimitError) as ei:
        await p.chat([Message(Role.USER, "hi")], model="gpt-4o-mini")
    assert ei.value.retry_after_sec == 12.0
    assert ei.value.retryable is True


@pytest.mark.asyncio
async def test_http_500_raises_server_error_and_is_retryable() -> None:
    transport, _ = _mock({"error": "oops"}, status=503)
    p = GoogleProvider(api_key="k", transport=transport)
    with pytest.raises(LLMServerError) as ei:
        await p.chat([Message(Role.USER, "hi")], model="gemini-2.5-flash")
    assert ei.value.status_code == 503
    assert ei.value.retryable is True


@pytest.mark.asyncio
async def test_http_400_raises_bad_request_not_retryable() -> None:
    transport, _ = _mock({"error": "schema"}, status=400)
    p = AnthropicProvider(api_key="sk-test", transport=transport)
    with pytest.raises(LLMBadRequestError) as ei:
        await p.chat([Message(Role.USER, "hi")], model="claude-opus-4-7")
    assert ei.value.retryable is False


# ── Router ──────────────────────────────────────────────────────────


def test_infer_provider_dispatches_on_prefix() -> None:
    assert infer_provider("claude-opus-4-7") == "anthropic"
    assert infer_provider("gpt-4o-mini") == "openai"
    assert infer_provider("o3-mini") == "openai"
    assert infer_provider("gemini-2.5-flash") == "google"
    with pytest.raises(ValueError):
        infer_provider("llama-3.1-70b")


def test_provider_constructor_rejects_empty_key() -> None:
    with pytest.raises(ValueError):
        AnthropicProvider(api_key="")
    with pytest.raises(ValueError):
        OpenAIProvider(api_key="")
    with pytest.raises(ValueError):
        GoogleProvider(api_key="")
