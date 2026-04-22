"""排程（Schedule）Pydantic schemas。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ScheduleBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    # 單選（舊版）與多選（新版）任一皆可；前端優先送 node_ids 多選清單
    node_id: Optional[str] = None
    node_ids: list[str] = Field(default_factory=list)
    repeat_type: str = Field("ONCE", description="ONCE / DAILY / WEEKLY / MONTHLY")
    repeat_config: Optional[str] = None
    # 使用者輸入的「本地時間」字串 `YYYY-MM-DDTHH:mm`（由前端 datetime-local 產生）
    # 或 ISO 8601；後端一律視為客戶端當地時間再計算 next_run_at
    next_run_at: datetime
    # 執行環境：docker / local；預設 docker
    execution_mode: str = "docker"


class ScheduleCreate(ScheduleBase):
    active: bool = True


class ScheduleUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    node_id: Optional[str] = None
    node_ids: Optional[list[str]] = None
    repeat_type: Optional[str] = None
    repeat_config: Optional[str] = None
    next_run_at: Optional[datetime] = None
    active: Optional[bool] = None
    execution_mode: Optional[str] = None


class ScheduleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    node_id: str
    # 多選清單（可能只有一個元素）；前端用這個判斷是否要顯示「+N」
    node_ids: list[str] = Field(default_factory=list)
    project_id: str
    # 額外塞入節點標題方便前端列表顯示（由 router 手動填入）
    node_title: Optional[str] = None
    # 多選時每個 node 的 title（照 node_ids 順序）
    node_titles: list[str] = Field(default_factory=list)
    repeat_type: str
    repeat_config: Optional[str] = None
    next_run_at: datetime
    last_run_at: Optional[datetime] = None
    last_report_id: Optional[str] = None
    active: bool
    execution_mode: str = "docker"
    created_at: datetime
    updated_at: datetime
