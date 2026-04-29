"""Screenshot diff 基準圖（per-step）。

每個 step 在 testcase_contents.steps_json 內有自己的 ``id``（UUID），這張表用該 UUID
當主鍵儲存基準圖 URL 與容忍度。執行時 ``AssertScreenshot.Match`` 會：
  - baseline 不存在 → 把當下截圖上傳當 baseline → step PASS（auto-save）
  - baseline 存在  → Pillow 像素 diff，diff% > threshold 就 FAIL，並上傳「紅色覆蓋」diff 圖

testcase_node_id 只是參考欄位（顯示「這 baseline 屬於哪個案例」）；FK 設 SET NULL
是為了刪除 testcase 時保留 baseline，待孤兒清理工作。
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.tenant import TenantScoped
from .base import Base


class StepScreenshotBaseline(TenantScoped, Base):
    __tablename__ = "step_screenshot_baselines"

    # step UUID（即 steps_json 內某個 step 的 ``id`` 欄位）
    step_uuid: Mapped[str] = mapped_column(String(36), primary_key=True)
    testcase_node_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("tree_nodes.id", ondelete="SET NULL"), nullable=True
    )
    # MinIO 對外 URL（/results/baselines/<step_uuid>.png）
    baseline_url: Mapped[str] = mapped_column(Text, nullable=False)
    # 像素差異百分比門檻；> 此值 step 才會 FAIL；預設 1.0%
    threshold_pct: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
