"""EmailVerificationToken — 自助註冊的 email 驗證 token。

流程:
  1. User 在登入頁切到「建立帳號」tab,填使用者名稱 / email / 帳號 / 密碼。
  2. Backend ``POST /api/auth/register`` 建 ``users`` row 但 ``is_active=False``,
     mint 一筆 token,透過 EmailConfig 寄出 ``/?verify_token=<token>`` 連結。
  3. User 點連結 → 前端讀 URL param → ``POST /api/auth/register/verify``。
  4. Backend 驗證:token 存在 / 未過期 / 未使用 → 把 ``users.is_active=True`` +
     標記 ``used_at``。User 之後可登入,但 ``organization_id`` / ``role_id`` 都
     是 NULL,所以只能呼叫 ``/api/auth/me``;superuser 在「設定 → 專案協作成
     員」手動指派 org+role 才能完全可用。

設計決策:
  * Token = ``secrets.token_urlsafe(32)``,~43 字元 >256 bit 猜測難度
  * 24 小時過期(比 forgot-password 的 1 小時長,因為 email 可能延遲)
  * ``user_id`` FK ON DELETE CASCADE — 刪 user 也刪 token,沒孤兒
  * Token used 後標 ``used_at`` 但不刪 row,給審計用(查「誰是何時驗證的」)
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4()),
    )
    token: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 寄信送達的 email(存下來 audit 用)
    email_sent_to: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    requested_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
