from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.execution_step_log import ExecutionStepLog
from app.services.storage_service import save_screenshot

router = APIRouter()


# 15. POST /api/upload/screenshot
@router.post("/upload/screenshot")
async def upload_screenshot(
    file: UploadFile = File(...),
    step_id: str = Form(...),
    screenshot_type: str = Form(..., pattern="^(pre|post)$"),
    db: AsyncSession = Depends(get_db),
):
    """
    內部 API：自動化腳本（Playwright）截圖後上傳此端點。
    圖片存入 PIC 資料夾，URL 寫回 execution_steps_log。
    """
    result = await db.execute(
        select(ExecutionStepLog).where(ExecutionStepLog.id == step_id)
    )
    step = result.scalar_one_or_none()
    if step is None:
        raise HTTPException(status_code=404, detail="Step log not found")

    url = await save_screenshot(file)

    if screenshot_type == "pre":
        step.pre_screenshot_url = url
    else:
        step.post_screenshot_url = url

    await db.flush()
    return {"url": url, "step_id": step_id, "type": screenshot_type}
