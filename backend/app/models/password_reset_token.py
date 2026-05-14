"""PasswordResetToken — 透過 email 寄送的重置連結 token。

流程:
  1. 使用者在登入頁「忘記密碼」輸入 username + email。
  2. Backend 對 (username, email) match 一筆 active user 後 mint 一筆 token,
     並透過 EmailConfig 寄出包含 ``/?reset_token=<token>`` 的連結。
  3. 使用者點連結 → 前端讀 URL param → 帶 token + new_password 呼叫
     ``POST /auth/reset-password``。
  4. 後端驗證:token 存在 / 未過期 / 未使用 → 寫入新密碼 + 標記
     ``used_at`` + 把 ``users.must_change_password`` 設 False(代表使用者
     已自主修改,不需再 force 一次)。

設計決策:
  * Token 是 ``secrets.token_urlsafe(32)``,~43 字元,猜測難度 > 256 bit。
  * 一律 1 小時過期 (``expires_at``);超時 token 仍留 row 以利 audit,
    過期後 cron / 手動掃描可清。
  * username 是 String FK (非 cascade delete),刪 user 不會拖刪 token row;
    ``user`` 還在但 inactive 也允許 mint(privacy:不洩露帳號狀態),
    但實際 reset 時會檢查 ``is_active``。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4()),
    )
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    username: Mapped[str] = mapped_column(
        String(80), ForeignKey("users.username", ondelete="CASCADE"), nullable=False, index=True,
    )
    # v1.1.7 Phase 3 shadow column。Phase 7 換 PK 時 user_id 升格成 FK。
    user_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True,
    )
    # 寄信送達的 email(存下來 audit 用,跟 user.email 可能不同 — 例如使用者
    # 後續又換了 email,我們仍能追溯這封信當時是寄到哪裡)。
    email_sent_to: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # 觸發來源 IP(rate-limit 已經卡 IP,這裡只做 audit 紀錄)
    requested_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
