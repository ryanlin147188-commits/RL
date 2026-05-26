"""Shell 執行 API — 僅供 superuser 跑明確 whitelist 內的維運指令。

歷史:v1.1.13 之前只擋「已登入」,且 ``shell=True``,任何登入用戶都可 RCE。
本檔已於 v1.1.13 強化為:
  * Depends(current_active_superuser) — 一般使用者直接 403
  * 第一個 token 必須命中 ``_ALLOWED_COMMANDS`` whitelist
  * ``shlex.split()`` + ``shell=False`` — 不再經由 shell 解析
  * 例外不洩漏 stacktrace 給 client(只給 generic 訊息,完整 traceback 進 server log)

AuditMiddleware 會自動記下 POST /api/shell/exec 的 user / ip / status,
另外在這裡用 logger.info 記下實際 command,供事後稽核。
"""
from __future__ import annotations

import logging
import shlex
import subprocess

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth.dependencies import current_active_superuser
from app.models import User

logger = logging.getLogger(__name__)
router = APIRouter()

# 第一個 token(可執行檔名)必須在這份白名單內。其後參數仍以 shlex.split 解析、
# 不經 shell,所以無法用 ``;`` / ``&&`` / 反引號 等串接其他指令。
_ALLOWED_COMMANDS = frozenset({
    "alembic",
    "python",
    "python3",
    "pytest",
})


class ShellRequest(BaseModel):
    command: str
    timeout: int = 60


@router.post("/shell/exec", tags=["Shell"])
async def shell_exec(
    req: ShellRequest,
    request: Request,
    user: User = Depends(current_active_superuser),
):
    try:
        argv = shlex.split(req.command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"指令解析失敗:{exc}") from exc
    if not argv:
        raise HTTPException(status_code=400, detail="command 不可為空")

    head = argv[0]
    if head not in _ALLOWED_COMMANDS:
        logger.warning(
            "shell_exec rejected (not in whitelist): user=%s head=%s",
            user.username,
            head,
        )
        raise HTTPException(
            status_code=403,
            detail=f"指令 `{head}` 不在白名單內,允許的指令:{sorted(_ALLOWED_COMMANDS)}",
        )

    logger.info("shell_exec: user=%s argv=%s", user.username, argv)
    timeout = max(1, min(req.timeout, 300))

    try:
        result = subprocess.run(  # noqa: S603 — argv 已 whitelist 過,且 shell=False
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "return_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        logger.warning("shell_exec timeout: user=%s argv=%s timeout=%s", user.username, argv, timeout)
        return {"stdout": "", "stderr": f"指令超時({timeout}s)", "return_code": -1}
    except FileNotFoundError:
        raise HTTPException(status_code=400, detail=f"找不到可執行檔:{head}")
    except Exception:  # noqa: BLE001 - subprocess 各種底層錯誤統一回 500
        logger.exception("shell_exec failed: user=%s argv=%s", user.username, argv)
        raise HTTPException(status_code=500, detail="指令執行失敗,請查看 server log")
