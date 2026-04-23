"""專案層級設定（環境變數 + 設備資訊）對應 schema。

兩個資源都採「PUT 整批替換」模式：前端把整張表存起來，要存就一次 PUT 整個 list；
資料庫端執行 delete-then-insert，避免 UI 端維護局部 diff 的複雜度。
"""
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.project_device import DevicePlatform


# ── 環境變數 ─────────────────────────────────────────────
class EnvVarItem(BaseModel):
    """單一環境變數；前端送 PUT 時每個元素都用此格式。"""
    name: str = Field(..., min_length=1, max_length=100)
    value: str = Field(default="", max_length=64_000)
    description: Optional[str] = Field(default=None, max_length=500)


class EnvVarResponse(EnvVarItem):
    model_config = ConfigDict(from_attributes=True)
    id: str


class EnvVarsListResponse(BaseModel):
    project_id: str
    items: list[EnvVarResponse]


# ── 設備資訊 ─────────────────────────────────────────────
class DeviceItem(BaseModel):
    """單一設備（Android / iOS 虛擬裝置）。"""
    label: str = Field(..., min_length=1, max_length=100)
    platform: DevicePlatform
    platform_version: Optional[str] = Field(default=None, max_length=20)
    device_name: Optional[str] = Field(default=None, max_length=100)
    avd_name: Optional[str] = Field(default=None, max_length=100)
    udid: Optional[str] = Field(default=None, max_length=100)
    automation_name: Optional[str] = Field(default=None, max_length=50)
    extra_caps_json: Optional[dict[str, Any]] = None


class DeviceResponse(DeviceItem):
    model_config = ConfigDict(from_attributes=True)
    id: str


class DevicesListResponse(BaseModel):
    project_id: str
    items: list[DeviceResponse]
