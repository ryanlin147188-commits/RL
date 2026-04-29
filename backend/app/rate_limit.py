"""API rate limiting via slowapi。

策略：
- 登入端點按 IP 限制（防暴力破解）
- AI 生成端點按 user 限制（防止單一 user 燒爆 AI quota）
- 其餘端點掛 default 限制（防 abuser 打爆服務）

key 函式：優先用 JWT username（middleware 已塞 request.state.user_payload），
回退到遠端 IP（給尚未登入的請求用）。
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def _user_or_ip_key(request: Request) -> str:
    """限流的 key：登入後用 user:username；否則用 ip:<addr>。"""
    payload = getattr(request.state, "user_payload", None)
    if isinstance(payload, dict):
        sub = payload.get("sub")
        if sub:
            return f"user:{sub}"
    return f"ip:{get_remote_address(request)}"


# Default：每使用者每分鐘 600 次（≈ 10 req/sec），給瀏覽器頁面與正常 polling 預留充分餘裕
# headers_enabled=False：要打開的話每個 @limiter.limit 端點都必須加 `response: Response`
# 參數讓 slowapi 注入 X-RateLimit-* header；目前先只做核心節流，header 之後補。
limiter = Limiter(
    key_func=_user_or_ip_key,
    default_limits=["600/minute"],
    headers_enabled=False,
    # Disable in tests so the same IP can hit a 3/hour endpoint repeatedly
    # without false 429s. Production / staging keep limits on.
    enabled=os.environ.get("AUTOTEST_TEST_MODE", "").strip() not in ("1", "true", "True"),
)
