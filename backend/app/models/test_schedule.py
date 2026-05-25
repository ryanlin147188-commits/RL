"""TestSchedule(測試時程)ORM Model — v1.1.9 加。

代表測試規劃中的「階段 / 里程碑」(planning milestone):

* Sprint 1 (2026-05-01 ~ 2026-05-14)
* 冒煙測試 (2026-05-15)
* UAT (2026-05-16 ~ 2026-05-20)
* 上線前回歸 (2026-05-21 ~ 2026-05-25)

每筆有 start_date / end_date 兩個日期界定區間,前端用 Gantt 風格時間軸呈現。
跟 ``schedules`` 表(cron-style 自動執行)是完全不同概念 — schedule 是排
「跑」測試的觸發時點,test_schedule 是排「規劃」階段。

可選 status(PLANNED/IN_PROGRESS/DONE/DELAYED/CANCELLED)讓 user 標進度,
color 給前端 timeline bar 上色用。
"""
import enum
import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.tenant import Assignable, TenantScoped
from .base import Base


class TestScheduleStatus(str, enum.Enum):
    """測試時程狀態 — v1.1.9+ 與測試看版(TodoStatus)統一 5 值。

    歷史 (v1.1.9 之前): Planned / InProgress / Done / Delayed / Cancelled。
    現在: Todo / InProgress / InReview / Verified / Closed,跟 TodoStatus
    對齊,首頁行事曆 / 看版 / 時程三處狀態語意一致。

    Migration 0036 把舊值轉成新值:
        Planned   → Todo
        Delayed   → Todo(無對應的「延期」狀態;一律當待辦,user 可看開始 / 結束日)
        Done      → Verified
        Cancelled → Closed
    """
    TODO        = "Todo"        # 待辦
    IN_PROGRESS = "InProgress"  # 進行中
    IN_REVIEW   = "InReview"    # 待驗證
    VERIFIED    = "Verified"    # 已完成
    CLOSED      = "Closed"      # 關閉


class TestSchedule(Assignable, TenantScoped, Base):
    __tablename__ = "test_schedules"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[TestScheduleStatus] = mapped_column(
        Enum(TestScheduleStatus, values_callable=lambda x: [e.value for e in x], native_enum=False, length=20),
        default=TestScheduleStatus.TODO, nullable=False,
    )
    # bar 顏色 — 前端 timeline 用,只存 tailwind color name(blue/emerald/amber/rose/violet)
    # v1.1.9+ 前端會依 status 自動推 bar 色,本欄位作 fallback / 手動 override 用
    color: Mapped[str] = mapped_column(String(20), nullable=False, default="blue", server_default="blue")
    # 進度百分比 (0-100) — Gantt-style 規劃工具常見欄位,讓 user 可以手動標
    # 「目前完成度」獨立於 status(因為 status 是離散狀態,progress 是線性百分比)
    progress: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    # 連結到某個 TestRound(可空)— 把時程跟一輪測試綁,後續可在 TestRun 顯示
    linked_test_round_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("test_rounds.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
