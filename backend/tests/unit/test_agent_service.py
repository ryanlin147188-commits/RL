"""agent_service.send_message 單元測試(Phase 1a + 1b)。

不開真實 DB / LLM;用 monkeypatch 把:
* ``get_provider_for_chat`` 換成 _FakeProvider
* DB 用 _FakeDB:模仿 add/flush/refresh + execute(select Message/usage/max(seq))
* ``filter_tools_for_user`` 在沒 user.role 時走 superuser path,直接放行

Phase 1b 重點 assertion:
* 無 tool_call → 單輪 chat 後結束
* 有 tool_call → dispatch tool → 寫 tool message → 再 chat 一輪 → 收到 text 結束
* MAX_TOOL_ITERATIONS 到上限會強制 tools=None 收尾
* tool 未知 / 拒權 → 寫 tool message 含 error,LLM 下一輪能看到
"""
from __future__ import annotations

from typing import Any, Optional

import pytest

from app.agent.tools.base import Tool, ToolResult
from app.agent.tools.registry import REGISTRY
from app.llm.base import ChatResult, LLMProvider, ToolCall, Usage
from app.models.agent_session import AgentMessage, AgentSession
from app.models.agent_token_usage import AgentTokenUsage
from app.models.user import User
from app.services import agent_service


# ── fakes ───────────────────────────────────────────────────────────


class _ScriptedProvider(LLMProvider):
    """每次 chat 依序回 ``replies`` 裡的下一個 ChatResult。"""

    provider_name = "fake"

    def __init__(self, replies: list[ChatResult]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, *, model, system=None, tools=None,
                   max_tokens=4096, temperature=0.7, timeout=60.0,
                   cache_system_and_tools=True):
        self.calls.append(
            {
                "messages": list(messages),
                "model": model,
                "system": system,
                "tools": tools,
            }
        )
        if not self._replies:
            raise AssertionError("_ScriptedProvider replies exhausted")
        return self._replies.pop(0)


class _ScalarResult:
    def __init__(self, val):
        self._val = val

    def scalar(self):
        return self._val

    def scalar_one_or_none(self):
        return self._val


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.flushed = 0

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed += 1

    async def refresh(self, obj):
        return obj

    async def delete(self, obj):
        self.deleted.append(obj)

    async def get(self, model, key):
        # filter_tools_for_user 對 superuser bypass;此 get 不會被呼叫到
        return None

    async def execute(self, stmt):
        from sqlalchemy.sql import Select

        if not isinstance(stmt, Select):
            return _ScalarResult(None)

        col_names = [str(c) for c in stmt.exported_columns]

        if any("max" in n.lower() and "seq" in n.lower() for n in col_names):
            max_seq = max(
                (m.seq for m in self.added if isinstance(m, AgentMessage)),
                default=0,
            )
            return _ScalarResult(max_seq)

        if any("agent_messages" in n for n in col_names):
            msgs = [m for m in self.added if isinstance(m, AgentMessage)]
            msgs.sort(key=lambda m: m.seq)
            return _ScalarsResult(msgs)

        if any("agent_token_usage" in n for n in col_names):
            usages = [u for u in self.added if isinstance(u, AgentTokenUsage)]
            return _ScalarResult(usages[-1] if usages else None)

        return _ScalarResult(None)


def _make_session(model: str = "claude-opus-4-7") -> AgentSession:
    s = AgentSession()
    s.id = "sess-1"
    s.user_id = "user-A"
    s.organization_id = "org-X"
    s.title = None
    s.model = model
    s.system_prompt = "you are a QA agent"
    return s


def _make_user(superuser: bool = True) -> User:
    """superuser=True 走 filter_tools_for_user 的 bypass,免設 role/permissions。"""
    u = User()
    u.id = "user-A"
    u.organization_id = "org-X"
    u.is_superuser = superuser
    u.role_id = None
    return u


def _reply_text(text: str = "OK") -> ChatResult:
    return ChatResult(
        content_text=text,
        tool_calls=[],
        usage=Usage(input_tokens=10, output_tokens=2, cost_usd=0.0001),
        model="claude-opus-4-7",
        provider="fake",
        stop_reason="end_turn",
        raw_response_id=f"fake-{id(text)}",
    )


def _reply_tool(tc: ToolCall) -> ChatResult:
    return ChatResult(
        content_text="",
        tool_calls=[tc],
        usage=Usage(input_tokens=10, output_tokens=2, cost_usd=0.0001),
        model="claude-opus-4-7",
        provider="fake",
        stop_reason="tool_use",
        raw_response_id=f"fake-tool-{id(tc)}",
    )


# ── Phase 1a 行為保留 ──────────────────────────────────────────────


@pytest.fixture
def patch_llm_simple(monkeypatch):
    fake = _ScriptedProvider([_reply_text("好的,我了解了。")])

    async def fake_get_provider(db, model, *, organization_id):
        return fake

    monkeypatch.setattr(agent_service, "get_provider_for_chat", fake_get_provider)
    return fake


@pytest.mark.asyncio
async def test_send_message_no_tool_calls_runs_single_round(patch_llm_simple) -> None:
    db = _FakeDB()
    session = _make_session()
    user = _make_user()

    user_msg, assistant_msg, usage = await agent_service.send_message(
        db, session, user=user, content="hello"
    )

    msgs = [a for a in db.added if isinstance(a, AgentMessage)]
    assert len(msgs) == 2  # user + assistant
    assert user_msg.role == "user"
    assert assistant_msg.role == "assistant"
    assert assistant_msg.content == "好的,我了解了。"
    assert len(patch_llm_simple.calls) == 1  # 只 chat 一輪
    assert usage is not None


@pytest.mark.asyncio
async def test_send_message_auto_titles_session(patch_llm_simple) -> None:
    db = _FakeDB()
    session = _make_session()
    session.title = None
    user = _make_user()

    long_text = "請幫我跑一下這個專案最近的所有失敗測試案例" * 3
    await agent_service.send_message(db, session, user=user, content=long_text)

    assert session.title is not None
    assert session.title.endswith("...")
    assert len(session.title) <= 53


# ── Phase 1b:tool-use 迴圈 ────────────────────────────────────────


class _NoopTool(Tool):
    name = "noop_tool"
    description = "Does nothing useful."
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    casbin_permission = None
    requires_confirmation = False

    def __init__(self):
        self.called_with: list[dict] = []

    async def execute(self, ctx, **kwargs):
        self.called_with.append(kwargs)
        return ToolResult.ok('{"status":"noop_done"}')


@pytest.fixture
def noop_tool(monkeypatch):
    """臨時註冊 noop_tool;測試結束復原。"""
    tool = _NoopTool()
    # 用 monkeypatch 換 registry 內部狀態,避免污染其他測試
    REGISTRY.register(tool)
    yield tool
    REGISTRY.unregister(tool.name)


@pytest.mark.asyncio
async def test_send_message_dispatches_tool_then_completes(noop_tool, monkeypatch) -> None:
    """LLM 第 1 輪回 tool_use → 執行 noop → 第 2 輪回 text。"""
    tc = ToolCall(id="call-1", name="noop_tool", arguments={"x": 1})
    fake = _ScriptedProvider([_reply_tool(tc), _reply_text("查到了。")])

    async def fake_get_provider(db, model, *, organization_id):
        return fake

    monkeypatch.setattr(agent_service, "get_provider_for_chat", fake_get_provider)

    db = _FakeDB()
    session = _make_session()
    user = _make_user()

    user_msg, assistant_msg, usage = await agent_service.send_message(
        db, session, user=user, content="跑一下"
    )

    # tool 真的被呼叫
    assert len(noop_tool.called_with) == 1
    assert noop_tool.called_with[0] == {"x": 1}

    # 訊息順序:user → assistant(tool_use) → tool → assistant(text)
    msgs = sorted(
        [a for a in db.added if isinstance(a, AgentMessage)], key=lambda m: m.seq
    )
    roles = [m.role for m in msgs]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert msgs[1].tool_calls is not None and len(msgs[1].tool_calls) == 1
    assert msgs[2].tool_call_id == "call-1"
    assert msgs[2].content.startswith("{")  # tool 結果是 JSON
    assert msgs[3].content == "查到了。"
    assert assistant_msg.content == "查到了。"
    assert len(fake.calls) == 2  # 兩輪 LLM


@pytest.mark.asyncio
async def test_send_message_unknown_tool_writes_error_to_tool_message(monkeypatch) -> None:
    """LLM 喚不存在的 tool → 寫一條 tool message 帶錯誤,讓 LLM 看到後自我修正。"""
    tc = ToolCall(id="call-z", name="nonexistent_tool", arguments={})
    fake = _ScriptedProvider([_reply_tool(tc), _reply_text("抱歉,工具不存在。")])

    async def fake_get_provider(db, model, *, organization_id):
        return fake

    monkeypatch.setattr(agent_service, "get_provider_for_chat", fake_get_provider)

    db = _FakeDB()
    session = _make_session()
    user = _make_user()

    _, assistant_msg, _ = await agent_service.send_message(
        db, session, user=user, content="跑 x"
    )

    tool_msgs = [
        m for m in db.added if isinstance(m, AgentMessage) and m.role == "tool"
    ]
    assert len(tool_msgs) == 1
    assert "未知 tool" in tool_msgs[0].content
    assert assistant_msg.content == "抱歉,工具不存在。"


@pytest.mark.asyncio
async def test_send_message_caps_at_max_iterations(monkeypatch) -> None:
    """LLM 一直回 tool_use,到第 MAX 輪會被強制送 tools=None 結束。"""
    # 構造 MAX_TOOL_ITERATIONS+ 個 tool reply,確保最後一輪 LLM 不會再回 tool_use
    # (因為這層測試的 LLM 不會自己 inspect tools=None,要靠 ScriptedProvider 最後給 text)
    tc = ToolCall(id="loop", name="noop_tool", arguments={})
    replies = [_reply_tool(tc) for _ in range(agent_service.MAX_TOOL_ITERATIONS - 1)]
    replies.append(_reply_text("收尾。"))

    # 註冊 noop_tool(本測試前可能未註冊)
    if REGISTRY.get("noop_tool") is None:
        REGISTRY.register(_NoopTool())
    try:
        fake = _ScriptedProvider(replies)

        async def fake_get_provider(db, model, *, organization_id):
            return fake

        monkeypatch.setattr(agent_service, "get_provider_for_chat", fake_get_provider)

        db = _FakeDB()
        session = _make_session()
        user = _make_user()

        _, assistant_msg, _ = await agent_service.send_message(
            db, session, user=user, content="無止盡 loop"
        )

        # 最後一輪 LLM 看到 tools=None(強制收尾)
        assert fake.calls[-1]["tools"] is None
        assert assistant_msg.content == "收尾。"
        # 總共跑了 MAX_TOOL_ITERATIONS 輪
        assert len(fake.calls) == agent_service.MAX_TOOL_ITERATIONS
    finally:
        REGISTRY.unregister("noop_tool")


# ── guard ────────────────────────────────────────────────────────────


class _ProtectedTool(Tool):
    name = "protected_tool"
    description = "Requires high permission."
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    casbin_permission = "report.read"  # 假權限,非 superuser 無此 role 沒權限
    requires_confirmation = False

    async def execute(self, ctx, **kwargs):
        return ToolResult.ok("should not reach here")


@pytest.mark.asyncio
async def test_send_message_tool_permission_denied_writes_denial_message(monkeypatch) -> None:
    """非 superuser + 無 role.permissions → tool 不會執行,tool msg 帶拒絕原因。"""
    REGISTRY.register(_ProtectedTool())
    try:
        tc = ToolCall(id="call-p", name="protected_tool", arguments={})
        fake = _ScriptedProvider([_reply_tool(tc), _reply_text("好的,無權限。")])

        async def fake_get_provider(db, model, *, organization_id):
            return fake

        monkeypatch.setattr(agent_service, "get_provider_for_chat", fake_get_provider)

        db = _FakeDB()
        session = _make_session()
        user = _make_user(superuser=False)  # 一般 user,無 role_id

        _, assistant_msg, _ = await agent_service.send_message(
            db, session, user=user, content="跑 protected"
        )

        tool_msgs = [
            m for m in db.added if isinstance(m, AgentMessage) and m.role == "tool"
        ]
        assert len(tool_msgs) == 1
        assert "report.read" in tool_msgs[0].content  # 拒絕訊息含缺的權限名
        assert assistant_msg.content == "好的,無權限。"
    finally:
        REGISTRY.unregister("protected_tool")


# ── tool registry / filter ───────────────────────────────────────────


def test_registry_register_rejects_duplicate_name() -> None:
    t = _NoopTool()
    REGISTRY.register(t)
    try:
        with pytest.raises(ValueError, match="重複"):
            REGISTRY.register(_NoopTool())
    finally:
        REGISTRY.unregister(t.name)


def test_query_report_tool_is_registered_by_bootstrap() -> None:
    """import app.agent.tools 應該自動註冊 QueryReportTool。"""
    assert REGISTRY.get("query_recent_reports") is not None


@pytest.mark.asyncio
async def test_filter_tools_superuser_sees_all() -> None:
    from app.agent.guard import filter_tools_for_user
    from app.agent.tools.query_report import QueryReportTool

    db = _FakeDB()
    user = _make_user(superuser=True)
    tools = [QueryReportTool()]
    filtered = await filter_tools_for_user(db, user, tools)
    assert len(filtered) == 1


@pytest.mark.asyncio
async def test_filter_tools_unprivileged_hides_protected_tools() -> None:
    from app.agent.guard import filter_tools_for_user
    from app.agent.tools.query_report import QueryReportTool

    db = _FakeDB()
    user = _make_user(superuser=False)  # role_id None → granted set 為空
    tools = [QueryReportTool()]
    filtered = await filter_tools_for_user(db, user, tools)
    assert filtered == []  # QueryReportTool 需要 report.read,user 無此權限


# ── Phase 1c-1:非同步 tool 的 task_id 流動 ────────────────────────


class _AsyncFakeTool(Tool):
    """模擬 RunTestCaseTool — is_async=True,回 metadata 含 task_id。"""

    name = "async_fake_tool"
    description = "Fake async tool"
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    casbin_permission = None
    requires_confirmation = False
    is_async = True

    async def execute(self, ctx, **kwargs):
        return ToolResult.ok(
            '{"status":"queued","task_id":"task-abc-123"}',
            task_id="task-abc-123",
            status="queued",
        )


@pytest.mark.asyncio
async def test_send_message_concurrency_limit_writes_denial_to_tool_message(
    monkeypatch,
) -> None:
    """LLM 派 tool 達 per-user 上限 → tool message 帶 concurrency_limit_exceeded,
    LLM 看到後該收手。"""
    # Patch try_acquire_concurrency 直接回 (False, current)
    from app.agent import guard

    async def fake_acquire(user, tool):
        return False, tool.concurrency_limit_per_user

    monkeypatch.setattr(guard, "try_acquire_concurrency", fake_acquire)

    # Patch agent_service 內 import 名(已被引入)
    monkeypatch.setattr(agent_service, "try_acquire_concurrency", fake_acquire)

    # 給 noop_tool 設個 limit
    class _LimitedTool(Tool):
        name = "limited_tool"
        description = "limited"
        input_schema = {"type": "object", "properties": {}, "additionalProperties": False}
        casbin_permission = None
        requires_confirmation = False
        concurrency_limit_per_user = 3

        async def execute(self, ctx, **kwargs):
            return ToolResult.ok("should not reach")

    REGISTRY.register(_LimitedTool())
    try:
        tc = ToolCall(id="call-limit", name="limited_tool", arguments={})
        fake = _ScriptedProvider([_reply_tool(tc), _reply_text("好的,我等等再試。")])

        async def fake_get_provider(db, model, *, organization_id):
            return fake

        monkeypatch.setattr(agent_service, "get_provider_for_chat", fake_get_provider)

        db = _FakeDB()
        session = _make_session()
        user = _make_user()

        await agent_service.send_message(
            db, session, user=user, content="跑滿了再試一次"
        )

        tool_msgs = [
            m for m in db.added if isinstance(m, AgentMessage) and m.role == "tool"
        ]
        assert len(tool_msgs) == 1
        assert "concurrency_limit_exceeded" in tool_msgs[0].content or "已達 per-user 上限" in tool_msgs[0].content
    finally:
        REGISTRY.unregister("limited_tool")


@pytest.mark.asyncio
async def test_send_message_persists_task_id_from_async_tool_metadata(
    monkeypatch,
) -> None:
    """非同步 tool 的 ToolResult.metadata.task_id 應寫進 AgentMessage.task_id 欄,
    給前端 polling / WS 訂閱用。"""
    REGISTRY.register(_AsyncFakeTool())
    try:
        tc = ToolCall(id="call-async", name="async_fake_tool", arguments={})
        fake = _ScriptedProvider([_reply_tool(tc), _reply_text("已排程, task-abc-123。")])

        async def fake_get_provider(db, model, *, organization_id):
            return fake

        monkeypatch.setattr(agent_service, "get_provider_for_chat", fake_get_provider)

        db = _FakeDB()
        session = _make_session()
        user = _make_user()

        _, assistant_msg, _ = await agent_service.send_message(
            db, session, user=user, content="跑一下"
        )

        tool_msgs = [
            m for m in db.added if isinstance(m, AgentMessage) and m.role == "tool"
        ]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].task_id == "task-abc-123"
        # assistant 訊息應有 LLM 收尾的文字回覆
        assert "task-abc-123" in assistant_msg.content
    finally:
        REGISTRY.unregister("async_fake_tool")
