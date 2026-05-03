"""DB Connection Config Pydantic schemas — 取代前端 localStorage 存的 DB 設定。

password 在 DB 以 Fernet 加密儲存(透過 EncryptedString)。Response 預設不回
明文，只回 `has_password` 讓前端知道已有值；更新時空 password 表示保留原值。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class DbConfigBase(BaseModel):
    name: str
    db_type: str
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None  # 明文,後端落地會自動 Fernet 加密
    extra_options: Optional[str] = None
    custom_dsn: Optional[str] = None
    description: Optional[str] = None
    enabled: bool = True


class DbConfigCreate(DbConfigBase):
    project_id: Optional[str] = None


class DbConfigUpdate(BaseModel):
    name: Optional[str] = None
    db_type: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    extra_options: Optional[str] = None
    custom_dsn: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None


class DbConfigResponse(DbConfigBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    project_id: Optional[str] = None
    organization_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    has_password: bool = False
