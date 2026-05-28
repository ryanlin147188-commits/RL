"""Phase 2 路線 B 自主 Agent 單元測試。

聚焦:
* per-mode system prompt 與 max_iterations 對應
* budget cap 月度查詢 + 超限 raise
* AnalyzerRunRequest / PlannerRunRequest schema 驗證
* StepLogs tool 屬性 sanity
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.agent.tools.query_step_logs import QueryStepLogsTool
from app.services import agent_budget_service, agent_service


# ── per-mode prompt / iteration ──────────────────────────────────


def test_system_prompt_for_chat_mode_uses_default() -> None:
    assert agent_service.system_prompt_for("chat") == agent_service.DEFAULT_SYSTEM_PROMPT


def test_system_prompt_for_planner_mentions_test_design() -> None:
    p = agent_service.system_prompt_for("planner")
    assert "planner" in p.lower() or "測試案例" in p
    assert "繁體中文" in p


def test_system_prompt_for_analyzer_mentions_root_cause() -> None:
    p = agent_service.system_prompt_for("analyzer")
    assert "root cause" in p.lower() or "失敗" in p
    assert "query_step_logs" in p


def test_system_prompt_for_unknown_mode_falls_back_to_chat() -> None:
    assert agent_service.system_prompt_for("unknown") == agent_service.DEFAULT_SYSTEM_PROMPT


def test_max_iterations_chat_is_5() -> None:
    assert agent_service.max_iterations_for("chat") == 5


def test_max_iterations_planner_is_higher_than_chat() -> None:
    assert (
        agent_service.max_iterations_for("planner")
        > agent_service.max_iterations_for("chat")
    )


def test_max_iterations_analyzer_is_between_chat_and_planner() -> None:
    a = agent_service.max_iterations_for("analyzer")
    assert (
        agent_service.max_iterations_for("chat")
        < a
        <= agent_service.max_iterations_for("planner")
    )


# ── budget cap 邏輯 ──────────────────────────────────────────────


def test_month_start_utc_is_first_day_zero_oclock() -> None:
    fake_now = datetime(2026, 5, 28, 14, 32, 1)
    out = agent_budget_service._month_start_utc(fake_now)
    assert out == datetime(2026, 5, 1, 0, 0, 0)


@pytest.mark.asyncio
async def test_check_budget_zero_limit_skips_query(monkeypatch) -> None:
    """limit_usd <= 0 不該觸發 DB 查詢。"""
    queried = []

    async def fake_get_spend(db, *, organization_id, now=None):
        queried.append(1)
        return Decimal("0")

    monkeypatch.setattr(
        agent_budget_service, "get_month_to_date_spend", fake_get_spend
    )

    out = await agent_budget_service.check_budget(
        db=None, organization_id="org-A", limit_usd=0
    )
    assert out == Decimal("0")
    assert queried == []


@pytest.mark.asyncio
async def test_check_budget_under_limit_returns_spent(monkeypatch) -> None:
    async def fake_get_spend(db, *, organization_id, now=None):
        return Decimal("3.21")

    monkeypatch.setattr(
        agent_budget_service, "get_month_to_date_spend", fake_get_spend
    )
    out = await agent_budget_service.check_budget(
        db=None, organization_id="org-A", limit_usd=50.0
    )
    assert out == Decimal("3.21")


@pytest.mark.asyncio
async def test_check_budget_at_or_over_limit_raises(monkeypatch) -> None:
    async def fake_get_spend(db, *, organization_id, now=None):
        return Decimal("50.0001")

    monkeypatch.setattr(
        agent_budget_service, "get_month_to_date_spend", fake_get_spend
    )
    with pytest.raises(agent_budget_service.BudgetExceeded) as ei:
        await agent_budget_service.check_budget(
            db=None, organization_id="org-A", limit_usd=50.0
        )
    assert ei.value.organization_id == "org-A"
    assert ei.value.spent_usd == Decimal("50.0001")


def test_budget_limit_multiplier_for_autonomous_modes(monkeypatch) -> None:
    """planner / analyzer 走 chat × multiplier。"""
    from app import config as _cfg

    saved = (
        _cfg.settings.AGENT_BUDGET_USD_PER_MONTH,
        _cfg.settings.AGENT_AUTONOMOUS_BUDGET_MULTIPLIER,
    )
    _cfg.settings.AGENT_BUDGET_USD_PER_MONTH = 50.0
    _cfg.settings.AGENT_AUTONOMOUS_BUDGET_MULTIPLIER = 3.0
    try:
        assert agent_service._budget_limit_for_mode("chat") == 50.0
        assert agent_service._budget_limit_for_mode("planner") == 150.0
        assert agent_service._budget_limit_for_mode("analyzer") == 150.0
    finally:
        (
            _cfg.settings.AGENT_BUDGET_USD_PER_MONTH,
            _cfg.settings.AGENT_AUTONOMOUS_BUDGET_MULTIPLIER,
        ) = saved


# ── QueryStepLogsTool 屬性 sanity ───────────────────────────────


def test_query_step_logs_required_fields() -> None:
    tool = QueryStepLogsTool()
    assert "report_id" in tool.input_schema["required"]
    assert tool.casbin_permission == "report.read"
    assert tool.requires_confirmation is False


# ── schemas ──────────────────────────────────────────────────────


def test_planner_run_request_min_length_enforced() -> None:
    from pydantic import ValidationError

    from app.schemas.agent import PlannerRunRequest

    with pytest.raises(ValidationError):
        PlannerRunRequest(requirement_text="short")  # < 10


def test_planner_run_request_accepts_normal() -> None:
    from app.schemas.agent import PlannerRunRequest

    p = PlannerRunRequest(
        requirement_text="這個需求是要驗證使用者登入流程能用 Zoho SSO"
    )
    assert p.requirement_text.startswith("這個需求")
    assert p.project_id is None


def test_analyzer_run_request_requires_report_id() -> None:
    from pydantic import ValidationError

    from app.schemas.agent import AnalyzerRunRequest

    with pytest.raises(ValidationError):
        AnalyzerRunRequest(report_id="")
