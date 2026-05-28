"""Agent budget cap — Phase 2 風險紅線:防自主 agent 把錢燒爆。

Mechanism:
* 設 ``AGENT_BUDGET_USD_PER_MONTH`` env(預設 50 美元)
* send_message 開頭 / planner_run / analyzer_run 開頭呼叫 ``check_budget(org_id)``
* 邏輯:撈 agent_token_usage 表內該 org 自月初到現在的 cost_usd 總和
* 超過 → raise ``BudgetExceeded`` → router 轉 402 Payment Required(語意明確)

* 為什麼 per-org 而非 per-user:沿用 [[project-ai-agent-roadmap]] 決策,
  LLM key 跟成本歸屬在 org 層
* superuser 在 organization_id IS NULL 的情境(global default key)會走「無 org」
  路徑;此時的成本算進 organization_id IS NULL 的 row,獨立計
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_token_usage import AgentTokenUsage


class BudgetExceeded(Exception):
    """本月 LLM 用量超過上限。caller 應轉成 HTTP 402(Payment Required)。"""

    def __init__(
        self,
        *,
        organization_id: Optional[str],
        spent_usd: Decimal,
        limit_usd: Decimal,
    ) -> None:
        super().__init__(
            f"Organization {organization_id or '(global)'} 本月 LLM 用量"
            f" ${spent_usd:.4f} 已達上限 ${limit_usd:.2f},暫停 agent 對話直到下個月或調高 cap"
        )
        self.organization_id = organization_id
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd


def _month_start_utc(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def get_month_to_date_spend(
    db: AsyncSession,
    *,
    organization_id: Optional[str],
    now: Optional[datetime] = None,
) -> Decimal:
    """撈該 org 自月初到 now 的 cost_usd 總和。回 Decimal,DB 沒資料回 0。"""
    start = _month_start_utc(now)
    stmt = select(func.coalesce(func.sum(AgentTokenUsage.cost_usd), 0)).where(
        AgentTokenUsage.created_at >= start
    )
    if organization_id is None:
        stmt = stmt.where(AgentTokenUsage.organization_id.is_(None))
    else:
        stmt = stmt.where(AgentTokenUsage.organization_id == organization_id)
    val = (await db.execute(stmt)).scalar()
    return Decimal(str(val or 0))


async def check_budget(
    db: AsyncSession,
    *,
    organization_id: Optional[str],
    limit_usd: float,
    now: Optional[datetime] = None,
) -> Decimal:
    """超過上限 raise BudgetExceeded;否則回目前花的總額(給 caller 顯示)。

    limit_usd <= 0 → 不檢查(等同關閉);planner / analyzer 可以照需要傳更高
    limit 覆寫(自主 agent 本來就比 chat 燒得多)。
    """
    if limit_usd <= 0:
        return Decimal("0")
    spent = await get_month_to_date_spend(
        db, organization_id=organization_id, now=now
    )
    if spent >= Decimal(str(limit_usd)):
        raise BudgetExceeded(
            organization_id=organization_id,
            spent_usd=spent,
            limit_usd=Decimal(str(limit_usd)),
        )
    return spent
