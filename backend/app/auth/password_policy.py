"""密碼複雜度檢查(register / forgot-password / reset-password / change-password 共用)。

規則:
* 長度 ≥ 10
* 至少包含三類:大寫、小寫、數字、特殊字元(任三類)
* 不可是常見弱密碼(black list,涵蓋大部分自動化破解字典頭部)

不在這層擋 leak DB(haveibeenpwned k-anon API),因為內部部署不一定能對外。
若日後要加,接這支函式內即可。
"""
from __future__ import annotations

import re

from fastapi import HTTPException

_MIN_LENGTH = 10
_FORBIDDEN = frozenset(
    {
        # 高頻洩漏密碼字典(NIST SP800-63B 建議黑名單前段)
        "password", "password1", "password123", "passw0rd",
        "qwerty", "qwerty123", "qwertyuiop",
        "12345678", "123456789", "1234567890", "1qaz2wsx",
        "letmein", "welcome", "welcome1", "welcome123",
        "admin", "admin123", "administrator", "root", "toor",
        "iloveyou", "monkey", "dragon", "football", "sunshine",
        "abc123", "abcd1234", "111111", "000000", "121212",
        "changeme", "changeme123", "test1234", "default",
    }
)


def _categories(pw: str) -> int:
    cats = 0
    if re.search(r"[A-Z]", pw):
        cats += 1
    if re.search(r"[a-z]", pw):
        cats += 1
    if re.search(r"\d", pw):
        cats += 1
    if re.search(r"[^A-Za-z0-9]", pw):
        cats += 1
    return cats


def validate_or_raise(password: str, *, field_name: str = "password") -> None:
    """驗證密碼複雜度,不合格 raise HTTPException(422)。讓 register / reset 等 endpoint
    直接掛上去就有保護。"""
    pw = password or ""
    if len(pw) < _MIN_LENGTH:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "password_too_short",
                "field": field_name,
                "message": f"密碼至少 {_MIN_LENGTH} 字元",
            },
        )
    if _categories(pw) < 3:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "password_too_simple",
                "field": field_name,
                "message": "密碼需包含至少 3 類:大寫 / 小寫 / 數字 / 特殊字元",
            },
        )
    if pw.lower() in _FORBIDDEN:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "password_forbidden",
                "field": field_name,
                "message": "此密碼太常見,請改用更獨特的密碼",
            },
        )
