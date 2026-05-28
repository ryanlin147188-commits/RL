"""PendingAction service + Phase 1c-2 confirm flow 單元測試。

聚焦:
1. service.mark_approved / mark_rejected 狀態轉換
2. expired 檢查
3. agent_service dispatch:requires_confirmation=True → 寫 PendingAction + placeholder
4. _FakeDB 覆蓋足夠模擬 select(AgentMessage where pending_action_id)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from app.agent.tools.base import Tool, ToolResult
from app.agent.tools.registry import REGISTRY
from app.llm.base import ChatResult, LLMProvider, ToolCall, Usage
from app.models.agent_session import AgentMessage, AgentSession
from app.models.agent_token_usage import AgentTokenUsage
from app.models.pending_action import (
    PENDING_STATUS_APPROVED,
    PENDING_STATUS_EXPIRED,
    PENDING_STATUS_PENDING,
    PENDING_STATUS_REJECTED,
    PendingAction,
)
from app.models.user import User
from app.services import agent_service, pending_action_service


# ── fakes(extend test_agent_service 的 fake) ──────────────────────


class _ScriptedProvider(LLMProvider):
    provider_name = "fake"

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    async def chat(self, messages, *, model, system=None, tools=None,
                   max_tokens=4096, temperature=0.7, timeout=60.0,
                   cache_system_and_tools=True):
        self.calls.append({"system": system, "tools": tools})
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
    """擴充版 fake:增加 PendingAction + AgentMessage by pending_action_id 支援。"""

    def __init__(self):
        self.added = []
        self.flushed = 0

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed += 1

    async def refresh(self, obj):
        return obj

    async def get(self, model, key):
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

        if any("pending_actions" in n for n in col_names):
            rows = [r for r in self.added if isinstance(r, PendingAction)]
            return _ScalarResult(rows[-1] if rows else None)

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
    s.title = "t"
    s.model = model
    s.system_prompt = "you are a QA agent"
    return s


def _make_user() -> User:
    u = User()
    u.id = "user-A"
    u.organization_id = "org-X"
    u.is_superuser = True
    u.role_id = None
    return u


def _make_pending(
    status: str = PENDING_STATUS_PENDING,
    expires_at: datetime | None = None,
) -> PendingAction:
    p = PendingAction()
    p.id = "pending-1"
    p.session_id = "sess-1"
    p.user_id = "user-A"
    p.tool_call_id = "call-x"
    p.tool_name = "destructive_tool"
    p.arguments = {"x": 1}
    p.status = status
    p.summary = "destructive_tool(x=1)"
    p.created_at = datetime.utcnow() - timedelta(minutes=1)
    p.expires_at = expires_at or (datetime.utcnow() + timedelta(minutes=30))
    p.resolved_at = None
    return p


# ── service unit tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_approved_changes_status_and_sets_resolved_at() -> None:
    p = _make_pending()
    db = _FakeDB()
    await pending_action_service.mark_approved(db, p)
    assert p.status == PENDING_STATUS_APPROVED
    assert p.resolved_at is not None


@pytest.mark.asyncio
async def test_mark_rejected_changes_status() -> None:
    p = _make_pending()
    db = _FakeDB()
    await pending_action_service.mark_rejected(db, p)
    assert p.status == PENDING_STATUS_REJECTED
    assert p.resolved_at is not None


@pytest.mark.asyncio
async def test_approve_already_resolved_raises() -> None:
    p = _make_pending(status=PENDING_STATUS_APPROVED)
    db = _FakeDB()
    with pytest.raises(pending_action_service.PendingActionAlreadyResolved):
        await pending_action_service.mark_approved(db, p)


@pytest.mark.asyncio
async def test_approve_expired_raises() -> None:
    p = _make_pending(expires_at=datetime.utcnow() - timedelta(minutes=1))
    db = _FakeDB()
    with pytest.raises(pending_action_service.PendingActionExpired):
        await pending_action_service.mark_approved(db, p)


@pytest.mark.asyncio
async def test_mark_expired_if_due_only_touches_pending() -> None:
    p = _make_pending(expires_at=datetime.utcnow() - timedelta(seconds=1))
    db = _FakeDB()
    await pending_action_service.mark_expired_if_due(db, p)
    assert p.status == PENDING_STATUS_EXPIRED


@pytest.mark.asyncio
async def test_mark_expired_if_due_noop_when_already_resolved() -> None:
    p = _make_pending(
        status=PENDING_STATUS_APPROVED,
        expires_at=datetime.utcnow() - timedelta(minutes=10),
    )
    db = _FakeDB()
    await pending_action_service.mark_expired_if_due(db, p)
    assert p.status == PENDING_STATUS_APPROVED  # 不該改


# ── send_message 看到 requires_confirmation=True 的整段流程 ─────────


class _DestructiveTool(Tool):
    name = "destructive_tool"
    description = "Pretend to do something destructive"
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    casbin_permission = None
    requires_confirmation = True
    is_async = False
    concurrency_limit_per_user = None

    async def execute(self, ctx, **kwargs):
        return ToolResult.ok('{"status":"actually_ran"}')


def _reply_tool(tc):
    return ChatResult(
        content_text="", tool_calls=[tc],
        usage=Usage(input_tokens=1, output_tokens=1, cost_usd=0),
        model="m", provider="fake", stop_reason="tool_use",
        raw_response_id=f"r{id(tc)}",
    )


def _reply_text(text):
    return ChatResult(
        content_text=text, tool_calls=[],
        usage=Usage(input_tokens=1, output_tokens=1, cost_usd=0),
        model="m", provider="fake", stop_reason="end_turn",
        raw_response_id=f"r{id(text)}",
    )


@pytest.mark.asyncio
async def test_send_message_with_confirm_tool_writes_pending_and_placeholder(
    monkeypatch,
) -> None:
    """LLM 喚 requires_confirmation tool → 應該:
    1. 寫 PendingAction(status=pending)
    2. 寫 placeholder tool message,content 是 JSON 含 awaiting_user_confirmation
    3. 不真執行 tool(_DestructiveTool.execute 不該被呼叫)
    4. LLM 收到 placeholder 後給文字回覆,告訴使用者去點按確認
    """
    REGISTRY.register(_DestructiveTool())
    try:
        tc = ToolCall(id="call-d", name="destructive_tool", arguments={"x": 1})
        fake = _ScriptedProvider(
            [_reply_tool(tc), _reply_text("此操作需要確認,請按下按鈕。")]
        )

        async def fake_get_provider(db, model, *, organization_id):
            return fake

        monkeypatch.setattr(agent_service, "get_provider_for_chat", fake_get_provider)

        db = _FakeDB()
        session = _make_session()
        user = _make_user()

        _, assistant_msg, _ = await agent_service.send_message(
            db, session, user=user, content="跑那個危險動作"
        )

        # 1. PendingAction 寫進去
        pendings = [a for a in db.added if isinstance(a, PendingAction)]
        assert len(pendings) == 1
        assert pendings[0].status == PENDING_STATUS_PENDING
        assert pendings[0].tool_name == "destructive_tool"
        assert pendings[0].arguments == {"x": 1}
        assert pendings[0].tool_call_id == "call-d"

        # 2. placeholder tool message 寫進去 + pending_action_id 連結
        tool_msgs = [
            m for m in db.added if isinstance(m, AgentMessage) and m.role == "tool"
        ]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].pending_action_id == pendings[0].id
        assert "awaiting_user_confirmation" in tool_msgs[0].content

        # 3. assistant follow-up 回給使用者
        assert "確認" in assistant_msg.content
    finally:
        REGISTRY.unregister("destructive_tool")


@pytest.mark.asyncio
async def test_approve_pending_action_executes_tool_and_updates_message(
    monkeypatch,
) -> None:
    """approve flow:確實執行 tool、update 原 tool message 為真結果、跑 follow-up。"""

    class _SpyTool(Tool):
        name = "spy_destr"
        description = "spy"
        input_schema = {"type": "object", "properties": {}, "additionalProperties": False}
        casbin_permission = None
        requires_confirmation = True
        is_async = False

        executed_with: list = []

        async def execute(self, ctx, **kwargs):
            _SpyTool.executed_with.append(kwargs)
            return ToolResult.ok('{"status":"executed"}')

    REGISTRY.register(_SpyTool())
    try:
        # Stage:user 已經 send 過 message → DB 已有 pending + placeholder tool message
        db = _FakeDB()
        session = _make_session()
        user = _make_user()

        # 模擬:預先放 PendingAction + placeholder tool message + 上游 assistant
        pending = _make_pending()
        pending.tool_name = "spy_destr"
        db.add(pending)

        placeholder_msg = AgentMessage(
            id="msg-tool-1",
            session_id="sess-1",
            role="tool",
            content='{"status":"awaiting_user_confirmation"}',
            tool_call_id=pending.tool_call_id,
            pending_action_id=pending.id,
            seq=1,
        )
        db.add(placeholder_msg)

        # LLM follow-up 一輪 text reply
        fake = _ScriptedProvider([_reply_text("好,我已經幫你跑完了。")])

        async def fake_get_provider(db, model, *, organization_id):
            return fake

        monkeypatch.setattr(agent_service, "get_provider_for_chat", fake_get_provider)

        action, tool_msg, follow_up = await agent_service.approve_pending_action(
            db, action=pending, user=user, session=session
        )

        # tool 真的被呼叫
        assert _SpyTool.executed_with == [{"x": 1}]
        # action status 變 approved
        assert action.status == PENDING_STATUS_APPROVED
        # tool message content 已更新為真結果
        assert placeholder_msg.content == '{"status":"executed"}'
        assert tool_msg is placeholder_msg
        # follow-up assistant 被寫
        assert follow_up.content == "好,我已經幫你跑完了。"
        assert follow_up.role == "assistant"
    finally:
        REGISTRY.unregister("spy_destr")


@pytest.mark.asyncio
async def test_reject_pending_action_releases_slot_and_writes_rejection(
    monkeypatch,
) -> None:
    """reject flow:不執行 tool、update message 為 user_rejected、release slot、follow-up。"""

    release_calls = []

    class _DummyTool(Tool):
        name = "destr_2"
        description = "x"
        input_schema = {"type": "object", "properties": {}, "additionalProperties": False}
        casbin_permission = None
        requires_confirmation = True
        is_async = False
        concurrency_limit_per_user = 2

        async def execute(self, ctx, **kwargs):
            raise AssertionError("reject 不該執行 tool")

    REGISTRY.register(_DummyTool())

    # mock release_concurrency
    from app.agent import guard as guard_mod

    async def fake_release(user, tool):
        release_calls.append((user.id, tool.name))

    monkeypatch.setattr(guard_mod, "release_concurrency", fake_release)
    monkeypatch.setattr(agent_service, "release_concurrency", fake_release)

    try:
        db = _FakeDB()
        session = _make_session()
        user = _make_user()

        pending = _make_pending()
        pending.tool_name = "destr_2"
        db.add(pending)

        placeholder = AgentMessage(
            id="msg-tool-2",
            session_id="sess-1",
            role="tool",
            content='{"status":"awaiting_user_confirmation"}',
            tool_call_id=pending.tool_call_id,
            pending_action_id=pending.id,
            seq=1,
        )
        db.add(placeholder)

        fake = _ScriptedProvider([_reply_text("收到,已取消。")])

        async def fake_get_provider(db, model, *, organization_id):
            return fake

        monkeypatch.setattr(agent_service, "get_provider_for_chat", fake_get_provider)

        action, tool_msg, follow_up = await agent_service.reject_pending_action(
            db, action=pending, user=user, session=session
        )

        assert action.status == PENDING_STATUS_REJECTED
        # tool message content 含 user_rejected
        assert "user_rejected" in placeholder.content
        # release_concurrency 有被呼叫(release destr_2 的 slot)
        assert ("user-A", "destr_2") in release_calls
        # follow-up assistant 被寫
        assert follow_up.content == "收到,已取消。"
    finally:
        REGISTRY.unregister("destr_2")


# ── _summarize_tool_call ───────────────────────────────────────────


def test_summarize_tool_call_truncates_long_arg_values() -> None:
    long_str = "x" * 200
    tc = ToolCall(id="i", name="my_tool", arguments={"text": long_str})
    summary = agent_service._summarize_tool_call(tc)
    assert "my_tool(" in summary
    assert "…" in summary  # 被截斷標記
    assert long_str not in summary  # 完整字串不該出現


def test_summarize_tool_call_caps_at_four_args() -> None:
    tc = ToolCall(
        id="i", name="t",
        arguments={f"a{i}": i for i in range(10)},
    )
    summary = agent_service._summarize_tool_call(tc)
    assert "共 10 個參數" in summary
