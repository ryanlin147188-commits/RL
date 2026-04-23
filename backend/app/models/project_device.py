"""專案層級設備資訊（Android emulator / iOS simulator）。

每個 row = 一個 Appium 可連到的虛擬裝置設定。執行時會自動把所有設備注入成 Robot
dict 變數（``&{DEVICE_<label>}``），可在 ``Mobile.Open`` 等步驟引用，例：

    Mobile.Open    http://appium:4723/wd/hub    ${DEVICE_pixel5.platformName}

或在前端步驟編輯器 input 欄位寫 ``${DEVICE_pixel5}`` 整包帶入 capabilities。
"""
import enum
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class DevicePlatform(str, enum.Enum):
    ANDROID = "ANDROID"
    IOS = "IOS"


class ProjectDevice(Base):
    __tablename__ = "project_devices"
    __table_args__ = (
        UniqueConstraint("project_id", "label", name="uq_device_project_label"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # 顯示名稱 / 變數命名後綴（會被注入為 ${DEVICE_<label>}，因此建議只用英數＋底線）
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    platform: Mapped[DevicePlatform] = mapped_column(Enum(DevicePlatform), nullable=False)
    # Appium capability：platformVersion（例 "13.0" / "17.4"）
    platform_version: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Appium capability：deviceName（例 "Pixel_5_API_33" / "iPhone 14 Pro Simulator"）
    device_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # Android 專屬：AVD 名稱（emulator 啟動參數）
    avd_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # iOS 專屬：Simulator UDID
    udid: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # automationName；空白時 backend 會依 platform 自動填 UiAutomator2 / XCUITest
    automation_name: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # 額外 capability（JSON 字典）；會與上述 capabilities 合併
    extra_caps_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
