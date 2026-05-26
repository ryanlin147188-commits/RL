from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class ProjectCreate(BaseModel):
    """建立測試專案 — name 必填，其餘為選填豐富欄位。"""
    name: str
    description: Optional[str] = None
    owner: Optional[str] = None
    status: Optional[str] = None
    start_date: Optional[str] = None
    target_date: Optional[str] = None
    tags: Optional[str] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    owner: Optional[str] = None
    status: Optional[str] = None
    start_date: Optional[str] = None
    target_date: Optional[str] = None
    tags: Optional[str] = None


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: Optional[str] = None
    owner: Optional[str] = None
    status: Optional[str] = None
    start_date: Optional[str] = None
    target_date: Optional[str] = None
    tags: Optional[str] = None
    # v1.1.10:前端「退出專案」section 用 organization_id 判斷專案是「自己 org 的」
    # 還是「被邀請進別人 org 的」(only 後者列出來給 user 退出)。
    organization_id: Optional[str] = None
    # v1.1.11:跨 org 協作場景下,前端右上 badge 顯示「該專案所屬 org 名稱」,
    # 不用再額外打 /api/orgs/{id} 拿名字。list_projects 跟 retrieve 都會 populate。
    organization_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime
