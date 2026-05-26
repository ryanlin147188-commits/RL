"""DB 欄位加密（Fernet 對稱加密）。

設計：
- master key 從環境變數 ``AUTOTEST_FERNET_KEY`` 讀取（base64-urlsafe，44 chars）
- 沒有提供時：用 JWT_SECRET 衍生一把（HKDF-SHA256），並 log 警告
- 加密格式：``"fernet:" + Fernet token``（前綴方便辨識，與舊版明文相容）
- 解密：偵測前綴；無前綴 → 視為 legacy 明文，直接回傳（升級期間用）

使用方式：
1. SQLAlchemy 欄位用 ``EncryptedString``：
       smtp_password: Mapped[Optional[str]] = mapped_column(EncryptedString(500))
   寫入時會自動加密、讀取時自動解密；ORM 看到的永遠是明文。

2. 也可在 service 層手動呼叫 ``encrypt_str()`` / ``decrypt_str()``。

正式部署：
- 強烈建議 ``AUTOTEST_FERNET_KEY`` 與 ``AUTOTEST_JWT_SECRET`` 走 KMS / Vault
- Key 一旦遺失，所有以該 key 加密的密碼都將無法復原（必須請使用者重新填）
- 後續 key rotation：新增 ``AUTOTEST_FERNET_KEY_ALT`` 支援雙鑰，逐筆 re-encrypt（本檔已預留 MultiFernet）
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy.types import String, TypeDecorator

from app.auth.security import JWT_SECRET

log = logging.getLogger(__name__)

_PREFIX = "fernet:"


def _derive_key_from_jwt_secret(secret: str) -> bytes:
    """JWT_SECRET → 32-byte AES key → urlsafe-b64 encoded（Fernet 要求格式）。"""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"autotest-fernet-derivation-salt",
        info=b"autotest-db-field-encryption",
    )
    raw = hkdf.derive(secret.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def _build_fernet() -> MultiFernet:
    keys: list[Fernet] = []
    primary = os.environ.get("AUTOTEST_FERNET_KEY")
    if primary:
        try:
            keys.append(Fernet(primary.encode() if isinstance(primary, str) else primary))
        except Exception as e:  # noqa: BLE001
            log.error("AUTOTEST_FERNET_KEY 無效：%s；回退到 JWT_SECRET 衍生", e)
    alt = os.environ.get("AUTOTEST_FERNET_KEY_ALT")
    if alt:
        try:
            keys.append(Fernet(alt.encode() if isinstance(alt, str) else alt))
        except Exception:  # noqa: BLE001
            pass
    if not keys:
        # 沒設環境變數 → 用 JWT_SECRET 衍生；正式環境請務必獨立設定
        log.warning(
            "AUTOTEST_FERNET_KEY 未設定，從 JWT_SECRET 衍生 Fernet key — "
            "正式部署請獨立設定一把以利 key rotation。"
        )
        keys.append(Fernet(_derive_key_from_jwt_secret(JWT_SECRET)))
    return MultiFernet(keys)


_fernet = _build_fernet()


def encrypt_str(plain: Optional[str]) -> Optional[str]:
    """加密；None / 空字串原樣回傳。"""
    if plain is None:
        return None
    if plain == "":
        return ""
    if isinstance(plain, str) and plain.startswith(_PREFIX):
        # 已加密，避免雙重加密
        return plain
    token = _fernet.encrypt(plain.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt_str(stored: Optional[str]) -> Optional[str]:
    """解密；None 原樣；無 prefix 視為 legacy 明文（升級期）；解密失敗 → 回傳原值並警告。"""
    if stored is None:
        return None
    if not isinstance(stored, str):
        return stored
    if not stored.startswith(_PREFIX):
        return stored  # legacy plaintext, 由呼叫端決定何時 re-encrypt
    token = stored[len(_PREFIX):]
    try:
        return _fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        log.warning("Fernet 解密失敗（key 可能變更或資料損壞）；回傳原始字串供使用者重新填寫")
        return stored


# ── SQLAlchemy TypeDecorator：模型欄位無感加密 ─────────────────────────

class EncryptedString(TypeDecorator):
    """模型用：寫入自動加密、讀取自動解密；底層 storage 是 String。

    用法：
        password: Mapped[Optional[str]] = mapped_column(EncryptedString(500), nullable=True)
    """
    impl = String
    cache_ok = True

    def __init__(self, length: int = 500, *args, **kwargs):
        super().__init__(length, *args, **kwargs)

    def process_bind_param(self, value, dialect):
        return encrypt_str(value)

    def process_result_value(self, value, dialect):
        return decrypt_str(value)
