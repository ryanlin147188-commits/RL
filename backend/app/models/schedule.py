"""排程（Schedule）ORM Model。

排程執行測試案例：
- 支援一次性、每天、每週、每月四種重複類型
- 儲存目標節點 id（通常是 TESTCASE，但後端會遞迴展開，所以支援 FEATURE / PAGE / SCENARIO 等父節點）
- next_run_at 由 scheduler 背景任務定期輪詢觸發，並在觸發後更新為下一次執行時間
"""
import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.tenant import TenantScoped
from .base import Base


class RepeatType(str, enum.Enum):
    ONCE = "ONCE"         # 單次執行（到 next_run_at 觸發一次後 active=False）
    DAILY = "DAILY"       # 每天相同時刻
    WEEKLY = "WEEKLY"     # 每週指定星期幾（repeat_config = "0,2,4" 代表週日/二/四；0=Sun, 6=Sat）
    MONTHLY = "MONTHLY"   # 每月指定某日（repeat_config = "15" 代表每月 15 號）


class Schedule(TenantScoped, Base):
    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # 目標節點（TESTCASE / SCENARIO / PAGE / PLATFORM / FEATURE；backend 會遞迴展開下面所有 TESTCASE）
    # 為了相容舊版，仍保留 node_id 當作「主要節點」；多選節點放在 node_ids_json 裡（JSON array）
    node_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tree_nodes.id", ondelete="CASCADE"), nullable=False
    )
    # 多選節點清單（JSON array of string）；若為 None/空陣列，就退化為只使用 node_id
    node_ids_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 同時紀錄 project_id，方便列表 / 查詢 / 刪除時過濾
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    repeat_type: Mapped[RepeatType] = mapped_column(
        Enum(RepeatType), default=RepeatType.ONCE, nullable=False
    )
    # 配合 repeat_type 的額外設定：
    #   WEEKLY  → 逗號分隔的星期 index (0-6)；例 "1,3,5"
    #   MONTHLY → 單一日數字串；例 "15"
    #   ONCE / DAILY → 空字串
    repeat_config: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 指定時間（一律存 UTC；前端傳入本地時間，後端轉 UTC）
    next_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_report_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 執行環境：docker（Celery 容器跑）/ local（本機 agent 跑）；scheduler_loop 背景觸發時會採用這個值
    execution_mode: Mapped[str] = mapped_column(String(16), default="docker", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
