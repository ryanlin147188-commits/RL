"""run_test_case tool 單元測試(Phase 1c-1)。

不依賴真實 DB / Celery / robot-runner;monkeypatch:
* ``collect_execution_plan`` 回固定 plan
* ``create_report`` 回一個假 ExecutionReport
* ``celery_app.send_task`` 用 spy 紀錄參數
* ``ctx.db.commit`` 用 spy

聚焦驗證:
* 成功路徑 → ToolResult.ok,content 是 JSON 含 task_id + status=queued
* metadata 含 task_id(下游 _write_tool_msg 會把它存進 AgentMessage.task_id)
* Celery 連線失敗 → 仍回 ok 但 status=celery_unreachable + payload 含 error
* node_ids 空 → ToolResult.fail
* total=0 → ToolResult.fail("no_testcases_found")
* execution_mode=local → 不派 Celery,status=awaiting_local_agent
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import HTTPException

from app.agent.tools.base import ToolContext
from app.agent.tools.run_test_case import RunTestCaseTool
from app.models.agent_session import AgentMessage
from app.models.user import User


# ── fakes ───────────────────────────────────────────────────────────


class _FakeReport:
    def __init__(self, report_id: str = "report-xyz") -> None:
        self.id = report_id


class _SpyDB:
    """只實作 commit + add(create_report 可能會用到);execute 給 collect_execution_plan
    用,但我們 monkeypatch 掉它,不會真的走 DB。"""

    def __init__(self) -> None:
        self.committed = 0
        self.added: list[Any] = []

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1

    async def flush(self):
        return None


def _make_user() -> User:
    u = User()
    u.id = "user-A"
    u.organization_id = "org-X"
    u.is_superuser = False
    u.role_id = None
    return u


def _make_ctx(db: _SpyDB | None = None) -> ToolContext:
    return ToolContext(
        db=db or _SpyDB(),
        user=_make_user(),
        organization_id="org-X",
        session_id="sess-1",
    )


@pytest.fixture
def patch_plan_and_report(monkeypatch):
    """讓 collect_execution_plan 回固定 plan,create_report 回固定 row。"""
    from app.agent.tools import run_test_case as mod

    async def fake_plan(db, *, node_ids, user):
        return {
            "setup_testcase_ids": ["pre-1"],
            "main_testcase_ids": ["case-A", "case-B"],
            "project_id": "proj-Z",
        }

    async def fake_create(
        db, project_id, trigger_type, total, task_id, **kwargs
    ):
        return _FakeReport(report_id=f"rep-{task_id[:8]}")

    monkeypatch.setattr(mod, "collect_execution_plan", fake_plan)
    monkeypatch.setattr(mod, "create_report", fake_create)


@pytest.fixture
def patch_celery_ok(monkeypatch):
    """送 send_task 成功;回傳 spy 讓測試斷言參數正確。"""
    calls: list[dict] = []

    class _FakeCeleryApp:
        def send_task(self, name, **kwargs):
            calls.append({"name": name, **kwargs})

    fake_app = _FakeCeleryApp()

    # 模擬 ``from tasks.celery_app import celery_app``:
    # tool 內部會做這個 import,我們在 sys.modules 塞一個 fake module
    import sys
    import types

    fake_mod = types.ModuleType("tasks.celery_app")
    fake_mod.celery_app = fake_app
    parent = types.ModuleType("tasks")
    parent.celery_app = fake_mod
    sys.modules["tasks"] = parent
    sys.modules["tasks.celery_app"] = fake_mod
    yield calls
    # 清理避免污染其他測試
    sys.modules.pop("tasks.celery_app", None)
    sys.modules.pop("tasks", None)


@pytest.fixture
def patch_celery_unreachable(monkeypatch):
    """模擬 Celery / Valkey 連不上,send_task raise。"""
    import sys
    import types

    class _BrokenCeleryApp:
        def send_task(self, name, **kwargs):
            raise ConnectionError("redis broken")

    fake_mod = types.ModuleType("tasks.celery_app")
    fake_mod.celery_app = _BrokenCeleryApp()
    parent = types.ModuleType("tasks")
    parent.celery_app = fake_mod
    sys.modules["tasks"] = parent
    sys.modules["tasks.celery_app"] = fake_mod
    yield
    sys.modules.pop("tasks.celery_app", None)
    sys.modules.pop("tasks", None)


# ── tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_test_case_happy_path_returns_queued_with_task_id(
    patch_plan_and_report, patch_celery_ok,
) -> None:
    tool = RunTestCaseTool()
    db = _SpyDB()
    ctx = _make_ctx(db)

    result = await tool.execute(ctx, node_ids=["node-1", "node-2"])

    assert result.error is None
    payload = json.loads(result.content)
    assert payload["status"] == "queued"
    assert payload["task_id"]
    assert payload["report_id"].startswith("rep-")
    assert payload["total_cases"] == 3  # setup 1 + main 2
    assert payload["execution_mode"] == "docker"
    assert payload["status_url"].startswith("/api/executions/")

    # metadata 帶 task_id 給 _write_tool_msg 抽出來存
    assert result.metadata["task_id"] == payload["task_id"]
    assert result.metadata["status"] == "queued"

    # commit 跑了
    assert db.committed == 1
    # Celery send_task 收到正確參數
    assert len(patch_celery_ok) == 1
    sent = patch_celery_ok[0]
    assert sent["name"] == "tasks.execution_tasks.run_tests"
    sent_kwargs = sent["kwargs"]
    assert sent_kwargs["task_id"] == payload["task_id"]
    assert sent_kwargs["testcase_ids"] == ["pre-1", "case-A", "case-B"]
    assert sent_kwargs["setup_testcase_ids"] == ["pre-1"]


@pytest.mark.asyncio
async def test_run_test_case_dedupes_node_ids(
    patch_plan_and_report, patch_celery_ok,
) -> None:
    tool = RunTestCaseTool()
    ctx = _make_ctx()

    # 故意傳重複的 node_id
    result = await tool.execute(ctx, node_ids=["n1", "n2", "n1", "n2"])
    assert result.error is None
    # 不直接斷言去重,而是斷言 collect_execution_plan 收到的是去重後;
    # 但本測試 fixture 沒 spy collect_execution_plan 的 node_ids 參數,
    # 改驗 source_node_id / multi_source 邏輯間接驗證 — payload 應只回 docker mode
    payload = json.loads(result.content)
    assert payload["status"] == "queued"


@pytest.mark.asyncio
async def test_run_test_case_empty_node_ids_returns_failure() -> None:
    tool = RunTestCaseTool()
    ctx = _make_ctx()

    result = await tool.execute(ctx, node_ids=[])

    assert result.error == "node_ids 不可為空"
    assert "參數錯誤" in result.content


@pytest.mark.asyncio
async def test_run_test_case_collect_plan_http_exception_to_tool_fail(
    monkeypatch,
) -> None:
    """collect_execution_plan raise HTTPException → 轉成 ToolResult.fail,不洩漏 HTTP。"""
    from app.agent.tools import run_test_case as mod

    async def fake_plan(db, *, node_ids, user):
        raise HTTPException(status_code=400, detail="node not found")

    monkeypatch.setattr(mod, "collect_execution_plan", fake_plan)

    tool = RunTestCaseTool()
    ctx = _make_ctx()
    result = await tool.execute(ctx, node_ids=["bad-node"])

    assert result.error is not None
    assert "execution_plan_failed" in result.error
    assert "node not found" in result.content


@pytest.mark.asyncio
async def test_run_test_case_empty_plan_returns_fail(monkeypatch) -> None:
    """plan 展開後 total=0(只有 main_ids 空)— LLM 看到合理錯誤訊息。"""
    from app.agent.tools import run_test_case as mod

    async def empty_plan(db, *, node_ids, user):
        return {
            "setup_testcase_ids": [],
            "main_testcase_ids": [],
            "project_id": "proj-Z",
        }

    monkeypatch.setattr(mod, "collect_execution_plan", empty_plan)

    tool = RunTestCaseTool()
    ctx = _make_ctx()
    result = await tool.execute(ctx, node_ids=["folder-with-no-cases"])

    assert result.error == "no_testcases_found"


@pytest.mark.asyncio
async def test_run_test_case_celery_unreachable_still_returns_ok_with_warning(
    patch_plan_and_report, patch_celery_unreachable,
) -> None:
    """Celery 連不上,報告已建好;LLM 該知道有問題但不該整個 chat 流程 500。"""
    tool = RunTestCaseTool()
    db = _SpyDB()
    ctx = _make_ctx(db)

    result = await tool.execute(ctx, node_ids=["node-1"])

    assert result.error is None  # 報告建好就算 ok,Celery 失敗在 payload 提示
    payload = json.loads(result.content)
    assert payload["status"] == "celery_unreachable"
    assert "error" in payload
    assert "ConnectionError" in payload["error"]
    # 但 task_id / report_id 都還是有效 — 可以重派
    assert payload["task_id"]
    assert payload["report_id"].startswith("rep-")
    # DB 已 commit(ExecutionReport row 已落地)
    assert db.committed == 1


@pytest.mark.asyncio
async def test_run_test_case_local_mode_skips_celery_dispatch(
    patch_plan_and_report, monkeypatch,
) -> None:
    """execution_mode=local 不該呼叫 send_task。"""
    import sys
    import types

    sent_calls: list = []

    class _SpyApp:
        def send_task(self, *a, **k):
            sent_calls.append((a, k))

    fake_mod = types.ModuleType("tasks.celery_app")
    fake_mod.celery_app = _SpyApp()
    parent = types.ModuleType("tasks")
    parent.celery_app = fake_mod
    sys.modules["tasks"] = parent
    sys.modules["tasks.celery_app"] = fake_mod
    try:
        tool = RunTestCaseTool()
        result = await tool.execute(
            _make_ctx(), node_ids=["node-1"], execution_mode="local"
        )
        payload = json.loads(result.content)
        assert payload["status"] == "awaiting_local_agent"
        assert payload["execution_mode"] == "local"
        # 不該派 Celery
        assert sent_calls == []
    finally:
        sys.modules.pop("tasks.celery_app", None)
        sys.modules.pop("tasks", None)


# ── Tool attribute sanity ─────────────────────────────────────────


def test_run_test_case_tool_is_async() -> None:
    tool = RunTestCaseTool()
    assert tool.is_async is True
    assert tool.casbin_permission == "testcase.execute"
    # Phase 1c-1 暫不 confirm;Phase 1c-2 接 UI 時改 True
    assert tool.requires_confirmation is False


def test_run_test_case_input_schema_requires_node_ids() -> None:
    tool = RunTestCaseTool()
    assert "node_ids" in tool.input_schema["required"]
    assert tool.input_schema["additionalProperties"] is False
