"""SprintLink Pydantic schemas — Sprint(TestSchedule)跨實體連結。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class SprintLinkBase(BaseModel):
    target_type: str  # 後端 router 校驗白名單
    target_id: str
    link_kind: str = "relates_to"
    note: Optional[str] = None


class SprintLinkCreate(SprintLinkBase):
    pass


class SprintLinkResponse(SprintLinkBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    schedule_id: str
    organization_id: Optional[str] = None
    created_at: Optional[datetime] = None
    created_by: Optional[str] = None
    # 給前端顯示方便,序列化時可填(由 router 用 registry 補上)
    target_title: Optional[str] = None
    target_code: Optional[str] = None
    # 標記是 legacy 單一連結(test_schedules.linked_target_*)還是真實 sprint_links row
    is_legacy: bool = False
