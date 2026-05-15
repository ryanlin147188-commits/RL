"""
Shell 執行 API — 讓測試資料頁面可以在後端環境執行終端機指令（如 seed 腳本、DB 指令）。
僅限已登入的管理員使用者呼叫。
"""
from __future__ import annotations

import subprocess

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.models import User

router = APIRouter()


class ShellRequest(BaseModel):
    command: str
    timeout: int = 60


@router.post("/shell/exec", tags=["Shell"])
async def shell_exec(req: ShellRequest, user: User = Depends(get_current_user)):
    try:
        result = subprocess.run(
            req.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=req.timeout,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "return_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"指令超時（{req.timeout}s）", "return_code": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "return_code": -1}
