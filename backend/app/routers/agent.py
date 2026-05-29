"""Agent session / message REST endpoints — Phase 1a。

只用 ``get_current_user``,不過 Casbin — 每個 user 預設可建自己的 session 與 chat
(沒 tool 就沒 destructive action,風險低)。Phase 1b 加 tool 後,destructive
tool 各自走 Casbin。

紅線:
* session / message 都 per-user 篩選,IDOR 防護由 ``get_session`` 內把關
* prompt injection 防護不在這層做(訊息內容是 user 自己輸入的,LLM 對自己的
  輸入暴露在 prompt injection 風險下時責任在 user;Phase 1b 接 tool 時才需要
  把 DB 撈出來的「他人資料」做 sanitize)
* LLM provider 沒設 key → 400(不是 500),讓 UI 引導 user 去設定頁
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


def _llm_error_detail(exc: LLMError) -> str:
    """LLM 上游錯誤訊息可能含敏感資訊(API key 後綴、internal endpoint、
    organization id),不直接回傳給 client。完整錯誤寫 server log,
    給 client 的是固定訊息 + 錯誤類別,讓前端可以分類顯示但無法窺探。"""
    log.exception("LLM call failed: %s: %s", type(exc).__name__, exc)
    return f"LLM 呼叫失敗({type(exc).__name__});詳細錯誤已寫入 server log。"

from app.auth.dependencies import get_current_user
from app.config import settings
from app.database import get_db
from app.llm.errors import LLMError
from app.models.agent_session import AgentMessage, AgentSession
from app.models.agent_token_usage import AgentTokenUsage
from app.models.user import User
from app.schemas.agent import (
    AgentMessageResponse,
    AgentSessionCreate,
    AgentSessionResponse,
    AgentSessionUpdate,
    AnalyzerRunRequest,
    AutonomousRunResponse,
    PlannerRunRequest,
    SendMessageRequest,
    SendMessageResponse,
    TokenUsageInfo,
)
from app.schemas.pending_action import (
    PendingActionResolveResponse,
    PendingActionResponse,
)
from app.services import (
    agent_budget_service,
    agent_service,
    memory_client,
    pending_action_service,
)

router = APIRouter()


def _session_to_response(s: AgentSession) -> dict:
    return {
        "id": s.id,
        "user_id": s.user_id,
        "organization_id": s.organization_id,
        "title": s.title,
        "model": s.model,
        "system_prompt": s.system_prompt,
        "memory_enabled": bool(getattr(s, "memory_enabled", True)),
        "active_skill_id": getattr(s, "active_skill_id", None),
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }


def _msg_to_response(
    m: AgentMessage, usage: Optional[AgentTokenUsage] = None
) -> dict:
    return {
        "id": m.id,
        "session_id": m.session_id,
        "role": m.role,
        "content": m.content,
        "tool_calls": m.tool_calls,
        "tool_call_id": m.tool_call_id,
        "task_id": m.task_id,
        "pending_action_id": m.pending_action_id,
        "seq": m.seq,
        "created_at": m.created_at,
        "usage": (
            {
                "provider": usage.provider,
                "model": usage.model,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
                "cost_usd": usage.cost_usd,
            }
            if usage
            else None
        ),
    }


async def _get_own_session_or_404(
    db: AsyncSession, session_id: str, user: User
) -> AgentSession:
    row = await agent_service.get_session(
        db, session_id=session_id, user_id=user.id
    )
    if row is None:
        raise HTTPException(404, "session 不存在或非本人所有")
    return row


# ── Budget / usage ────────────────────────────────────────────────


@router.get(
    "/agent/budget/status",
    tags=["AE · Agent"],
)
async def get_budget_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """本月該 org 的 LLM 用量與成本上限資訊。任何登入 user 都可看自己 org 的數字。

    回傳:
        {
          spent_usd, call_count, input_tokens, output_tokens,
          cache_read_tokens, cache_write_tokens,
          limit_usd_chat, limit_usd_autonomous, multiplier,
          month_start
        }
    """
    summary = await agent_budget_service.get_month_to_date_summary(
        db, organization_id=user.organization_id
    )
    chat_limit = float(settings.AGENT_BUDGET_USD_PER_MONTH)
    multiplier = float(settings.AGENT_AUTONOMOUS_BUDGET_MULTIPLIER)
    return {
        # Decimal 轉 str 避免 JSON 浮點誤差(前端可 Number(value) 後 toFixed)
        "spent_usd": str(summary["cost_usd_total"]),
        "call_count": summary["call_count"],
        "input_tokens": summary["input_tokens"],
        "output_tokens": summary["output_tokens"],
        "cache_read_tokens": summary["cache_read_tokens"],
        "cache_write_tokens": summary["cache_write_tokens"],
        "limit_usd_chat": chat_limit,
        "limit_usd_autonomous": chat_limit * multiplier,
        "autonomous_multiplier": multiplier,
        "month_start": summary["month_start"],
    }


# ── Sessions ────────────────────────────────────────────────────────


@router.post(
    "/agent/sessions",
    response_model=AgentSessionResponse,
    tags=["AE · Agent"],
    status_code=201,
)
async def create_session(
    payload: AgentSessionCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await agent_service.create_session(
        db,
        user_id=user.id,
        organization_id=user.organization_id,
        title=payload.title,
        model=payload.model,
        system_prompt=payload.system_prompt,
    )
    return _session_to_response(row)


@router.get(
    "/agent/sessions",
    response_model=list[AgentSessionResponse],
    tags=["AE · Agent"],
)
async def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await agent_service.list_sessions_for_user(
        db, user_id=user.id, limit=limit
    )
    return [_session_to_response(r) for r in rows]


@router.get(
    "/agent/sessions/{session_id}",
    response_model=AgentSessionResponse,
    tags=["AE · Agent"],
)
async def get_session(
    session_id: str = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _get_own_session_or_404(db, session_id, user)
    return _session_to_response(row)


@router.patch(
    "/agent/sessions/{session_id}",
    response_model=AgentSessionResponse,
    tags=["AE · Agent"],
)
async def update_session(
    payload: AgentSessionUpdate,
    session_id: str = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _get_own_session_or_404(db, session_id, user)
    if payload.title is not None:
        await agent_service.update_session_title(db, row, payload.title)
    if payload.memory_enabled is not None:
        row.memory_enabled = bool(payload.memory_enabled)
        await db.flush()
    return _session_to_response(row)


@router.delete(
    "/agent/sessions/{session_id}",
    status_code=204,
    tags=["AE · Agent"],
)
async def delete_session(
    session_id: str = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _get_own_session_or_404(db, session_id, user)
    await agent_service.delete_session(db, row)
    return None


# ── Messages ────────────────────────────────────────────────────────


@router.get(
    "/agent/sessions/{session_id}/messages",
    response_model=list[AgentMessageResponse],
    tags=["AE · Agent"],
)
async def list_messages(
    session_id: str = Path(...),
    limit: int = Query(200, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_own_session_or_404(db, session_id, user)
    msgs = await agent_service.list_messages(
        db, session_id=session_id, limit=limit
    )
    # Phase 1a 不附 usage(批量列訊息時 join 成本不大但不必要;單條 send 後才回)
    return [_msg_to_response(m) for m in msgs]


@router.post(
    "/agent/sessions/{session_id}/messages",
    response_model=SendMessageResponse,
    tags=["AE · Agent"],
)
async def send_message(
    payload: SendMessageRequest,
    session_id: str = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """送一條 user 訊息,同步呼叫 LLM,回傳 (user_msg, assistant_msg)。

    LLM 失敗(key 沒設 / 上游 4xx/5xx)轉成 400/502;不洩漏 stack trace。
    Phase 1c 會改成非同步 + WebSocket;Phase 1a 先求快速跑通。
    """
    session = await _get_own_session_or_404(db, session_id, user)
    try:
        user_msg, assistant_msg, usage = await agent_service.send_message(
            db, session, user=user, content=payload.content
        )
    except agent_budget_service.BudgetExceeded as e:
        # 402 Payment Required — 語意明確,前端可顯示「成本上限」提示
        raise HTTPException(402, str(e)) from e
    except ValueError as e:
        # 沒設 key / 未知 provider / model 前綴不認得
        raise HTTPException(400, str(e)) from e
    except LLMError as e:
        # 上游錯誤 — 401/429/5xx 統一翻成 502 Bad Gateway(自家不背鍋)
        status = 502 if e.retryable else 400
        raise HTTPException(
            status,
            _llm_error_detail(e),
        ) from e

    return SendMessageResponse(
        user_message=AgentMessageResponse(**_msg_to_response(user_msg)),
        assistant_message=AgentMessageResponse(
            **_msg_to_response(assistant_msg, usage=usage)
        ),
    )


# ── Phase 1c-2:Pending action(二次確認)endpoints ─────────────────


def _pending_to_response(p) -> dict:
    # 對外回傳時剝掉 __integrity__ HMAC,前端不需要看到也不該看到
    args = p.arguments or {}
    if isinstance(args, dict) and "__integrity__" in args:
        args = {k: v for k, v in args.items() if k != "__integrity__"}
    return {
        "id": p.id,
        "session_id": p.session_id,
        "user_id": p.user_id,
        "tool_call_id": p.tool_call_id,
        "tool_name": p.tool_name,
        "arguments": args,
        "status": p.status,
        "summary": p.summary,
        "created_at": p.created_at,
        "expires_at": p.expires_at,
        "resolved_at": p.resolved_at,
    }


@router.get(
    "/agent/sessions/{session_id}/pending-actions",
    response_model=list[PendingActionResponse],
    tags=["AE · Agent"],
)
async def list_pending_actions(
    session_id: str = Path(...),
    status: Optional[str] = Query(None, description="過濾 status:pending/approved/rejected/expired"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_own_session_or_404(db, session_id, user)
    rows = await pending_action_service.list_for_session(
        db, session_id=session_id, user_id=user.id, status=status
    )
    return [_pending_to_response(r) for r in rows]


async def _resolve_pending_or_404(
    db: AsyncSession, action_id: str, user: User
):
    """讀 PendingAction(只能本人的);順手 mark expired if due。回 PendingAction 或 raise 404。"""
    row = await pending_action_service.get_for_user(
        db, action_id=action_id, user_id=user.id
    )
    if row is None:
        raise HTTPException(404, "PendingAction 不存在或非本人所有")
    await pending_action_service.mark_expired_if_due(db, row)
    return row


@router.post(
    "/agent/sessions/{session_id}/pending-actions/{action_id}/approve",
    response_model=PendingActionResolveResponse,
    tags=["AE · Agent"],
)
async def approve_pending_action(
    session_id: str = Path(...),
    action_id: str = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_own_session_or_404(db, session_id, user)
    action = await _resolve_pending_or_404(db, action_id, user)
    if action.session_id != session_id:
        raise HTTPException(404, "PendingAction 不屬於此 session")

    try:
        action, tool_msg, follow_up = await agent_service.approve_pending_action(
            db, action=action, user=user, session=session
        )
    except pending_action_service.PendingActionExpired as e:
        raise HTTPException(422, str(e)) from e
    except pending_action_service.PendingActionAlreadyResolved as e:
        raise HTTPException(422, str(e)) from e
    except ValueError as e:
        # follow-up chat 找不到 provider
        raise HTTPException(400, str(e)) from e
    except LLMError as e:
        status_code = 502 if e.retryable else 400
        raise HTTPException(
            status_code, _llm_error_detail(e)
        ) from e

    return PendingActionResolveResponse(
        pending=PendingActionResponse(**_pending_to_response(action)),
        tool_message=_msg_to_response(tool_msg),
        assistant_message=_msg_to_response(follow_up),
    )


# ── v1.2.x:mem0 跨 session 長期記憶管理 ──────────────────────────


@router.get(
    "/agent/memories",
    tags=["AE · Agent"],
)
async def list_user_memories(
    user: User = Depends(get_current_user),
):
    """列出該使用者所有跨 session 記憶(GDPR-friendly 介面)。

    mem0 sidecar 不通(or 未啟用)時回空 list,前端 UI 也會顯示「未啟用」提示。
    """
    items = await memory_client.list_memories(
        organization_id=user.organization_id, user_id=user.id
    )
    return {
        "enabled": memory_client.is_enabled(),
        "count": len(items),
        "memories": items,
    }


@router.delete(
    "/agent/memories/{memory_id}",
    status_code=204,
    tags=["AE · Agent"],
)
async def delete_one_memory(
    memory_id: str = Path(...),
    user: User = Depends(get_current_user),
):
    # 縱深防禦:先 list 該 user/org namespace,確認 memory_id 真的屬於這個
    # namespace,再轉發給 sidecar。雖然 mem0 memory_id 是 UUID 不易枚舉,但
    # log / audit 洩漏 memory_id 後仍可能跨 namespace 刪 — fail-closed 比較穩妥。
    items = await memory_client.list_memories(
        organization_id=user.organization_id, user_id=user.id
    )
    owned_ids = {
        (m.get("id") or m.get("memory_id") or "") for m in (items or [])
    }
    if memory_id not in owned_ids:
        # 用 404 而非 403,不洩漏「該 memory_id 是否存在於別人 namespace」
        raise HTTPException(404, "memory not found")
    ok = await memory_client.delete_memory(memory_id=memory_id)
    if not ok:
        raise HTTPException(502, "mem0 sidecar 未啟用或刪除失敗")
    return None


@router.delete(
    "/agent/memories",
    status_code=204,
    tags=["AE · Agent"],
)
async def delete_all_user_memories(
    user: User = Depends(get_current_user),
):
    """清掉該使用者所有 mem0 記憶 — GDPR / 重新開始用。"""
    ok = await memory_client.delete_all_for_user(
        organization_id=user.organization_id, user_id=user.id
    )
    if not ok:
        raise HTTPException(502, "mem0 sidecar 未啟用或刪除失敗")
    return None


# ── Phase 2:自主 agent endpoints ────────────────────────────────


async def _start_autonomous_session(
    db: AsyncSession,
    user: User,
    *,
    mode: str,
    title: str,
    initial_user_content: str,
    model: Optional[str],
) -> tuple:
    """共用:建 mode session + 送 first user message + 回 (session, user_msg, assistant_msg)。

    BudgetExceeded / LLMError 由 caller 路由處理。
    """
    session = await agent_service.create_session(
        db,
        user_id=user.id,
        organization_id=user.organization_id,
        title=title,
        model=model,
        mode=mode,
    )
    user_msg, assistant_msg, _ = await agent_service.send_message(
        db, session, user=user, content=initial_user_content
    )
    return session, user_msg, assistant_msg


@router.post(
    "/agent/planner/run",
    response_model=AutonomousRunResponse,
    tags=["AE · Agent"],
    status_code=201,
)
async def run_planner(
    payload: PlannerRunRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """從需求文字啟動 planner agent。

    內部:
      1. 建立 mode=planner 的 session(system prompt 切換成 planner 版)
      2. 把 requirement_text + 可選 project_id 包成 first user message
      3. 跑 LLM(max_iterations=15)
      4. 回 (session, user_msg, assistant_msg)

    planner 不會自己派 run_test_case;但若 user 要,接續對話會走 confirm flow。
    """
    project_hint = (
        f"\n\n[Context] project_id={payload.project_id}" if payload.project_id else ""
    )
    initial = (
        f"請幫我設計這份需求的測試 scenarios:\n\n"
        f"{payload.requirement_text}{project_hint}"
    )
    try:
        session, user_msg, assistant_msg = await _start_autonomous_session(
            db,
            user,
            mode="planner",
            title=f"[Planner] {payload.requirement_text[:40]}",
            initial_user_content=initial,
            model=payload.model,
        )
    except agent_budget_service.BudgetExceeded as e:
        raise HTTPException(402, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except LLMError as e:
        status_code = 502 if e.retryable else 400
        raise HTTPException(
            status_code, _llm_error_detail(e)
        ) from e

    return AutonomousRunResponse(
        session=AgentSessionResponse(**_session_to_response(session)),
        initial_user_message=AgentMessageResponse(**_msg_to_response(user_msg)),
        assistant_message=AgentMessageResponse(**_msg_to_response(assistant_msg)),
    )


@router.post(
    "/agent/analyzer/run",
    response_model=AutonomousRunResponse,
    tags=["AE · Agent"],
    status_code=201,
)
async def run_analyzer(
    payload: AnalyzerRunRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """從 failed report_id 啟動 analyzer agent。LLM 會自動呼叫 query_step_logs
    撈失敗 step,推斷 root cause,給出修復建議。"""
    initial = (
        f"請幫我分析 execution_report ID={payload.report_id} 的失敗原因。"
        f" 先用 query_step_logs 撈 FAILED step,看 error_message 推斷 root cause"
        f"(flaky / locator / 後端錯 / 真 bug),最後給出條列建議。"
    )
    try:
        session, user_msg, assistant_msg = await _start_autonomous_session(
            db,
            user,
            mode="analyzer",
            title=f"[Analyzer] report {payload.report_id[:8]}",
            initial_user_content=initial,
            model=payload.model,
        )
    except agent_budget_service.BudgetExceeded as e:
        raise HTTPException(402, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except LLMError as e:
        status_code = 502 if e.retryable else 400
        raise HTTPException(
            status_code, _llm_error_detail(e)
        ) from e

    return AutonomousRunResponse(
        session=AgentSessionResponse(**_session_to_response(session)),
        initial_user_message=AgentMessageResponse(**_msg_to_response(user_msg)),
        assistant_message=AgentMessageResponse(**_msg_to_response(assistant_msg)),
    )


@router.post(
    "/agent/sessions/{session_id}/pending-actions/{action_id}/reject",
    response_model=PendingActionResolveResponse,
    tags=["AE · Agent"],
)
async def reject_pending_action(
    session_id: str = Path(...),
    action_id: str = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_own_session_or_404(db, session_id, user)
    action = await _resolve_pending_or_404(db, action_id, user)
    if action.session_id != session_id:
        raise HTTPException(404, "PendingAction 不屬於此 session")

    try:
        action, tool_msg, follow_up = await agent_service.reject_pending_action(
            db, action=action, user=user, session=session
        )
    except pending_action_service.PendingActionExpired as e:
        raise HTTPException(422, str(e)) from e
    except pending_action_service.PendingActionAlreadyResolved as e:
        raise HTTPException(422, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except LLMError as e:
        status_code = 502 if e.retryable else 400
        raise HTTPException(
            status_code, _llm_error_detail(e)
        ) from e

    return PendingActionResolveResponse(
        pending=PendingActionResponse(**_pending_to_response(action)),
        tool_message=_msg_to_response(tool_msg),
        assistant_message=_msg_to_response(follow_up),
    )
