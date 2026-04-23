"""專案層級設定 CRUD：環境變數 + 設備資訊。

- ``GET /api/projects/{id}/env-vars``：列出
- ``PUT /api/projects/{id}/env-vars``：整批替換（delete-then-insert，避免局部 diff 複雜度）
- ``GET /api/projects/{id}/devices``：列出
- ``PUT /api/projects/{id}/devices``：整批替換

執行測試時，``backend/tasks/execution_tasks.run_tests`` 會用同樣的 GET 取出
這兩個列表，傳遞給 robot_runner，由 _build_robot_file 注入成 Robot suite variable。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.project import Project
from app.models.project_device import ProjectDevice
from app.models.project_env_var import ProjectEnvVar
from app.schemas.project_settings import (
    DeviceItem,
    DevicesListResponse,
    EnvVarItem,
    EnvVarsListResponse,
)

router = APIRouter()


async def _ensure_project(db: AsyncSession, project_id: str) -> None:
    p = await db.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Project not found")


# ════════════════════════════════════════════════════════════════
# 環境變數
# ════════════════════════════════════════════════════════════════


@router.get("/projects/{project_id}/env-vars", response_model=EnvVarsListResponse)
async def list_env_vars(project_id: str, db: AsyncSession = Depends(get_db)):
    await _ensure_project(db, project_id)
    rows = (await db.execute(
        select(ProjectEnvVar)
        .where(ProjectEnvVar.project_id == project_id)
        .order_by(ProjectEnvVar.name)
    )).scalars().all()
    return EnvVarsListResponse(project_id=project_id, items=rows)


@router.put("/projects/{project_id}/env-vars", response_model=EnvVarsListResponse)
async def replace_env_vars(
    project_id: str,
    items: list[EnvVarItem],
    db: AsyncSession = Depends(get_db),
):
    """整批替換。重複的 name 會在資料庫層被 UNIQUE 擋下；前端應自己先去重。"""
    await _ensure_project(db, project_id)
    # 去重檢查（案 case-sensitive）
    seen: set[str] = set()
    for it in items:
        if it.name in seen:
            raise HTTPException(status_code=400, detail=f"變數名稱重複：{it.name}")
        seen.add(it.name)

    # delete-then-insert
    await db.execute(delete(ProjectEnvVar).where(ProjectEnvVar.project_id == project_id))
    for it in items:
        db.add(ProjectEnvVar(
            project_id=project_id,
            name=it.name,
            value=it.value,
            description=it.description,
        ))
    await db.flush()

    rows = (await db.execute(
        select(ProjectEnvVar)
        .where(ProjectEnvVar.project_id == project_id)
        .order_by(ProjectEnvVar.name)
    )).scalars().all()
    return EnvVarsListResponse(project_id=project_id, items=rows)


# ════════════════════════════════════════════════════════════════
# 設備資訊
# ════════════════════════════════════════════════════════════════


@router.get("/projects/{project_id}/devices", response_model=DevicesListResponse)
async def list_devices(project_id: str, db: AsyncSession = Depends(get_db)):
    await _ensure_project(db, project_id)
    rows = (await db.execute(
        select(ProjectDevice)
        .where(ProjectDevice.project_id == project_id)
        .order_by(ProjectDevice.label)
    )).scalars().all()
    return DevicesListResponse(project_id=project_id, items=rows)


@router.put("/projects/{project_id}/devices", response_model=DevicesListResponse)
async def replace_devices(
    project_id: str,
    items: list[DeviceItem],
    db: AsyncSession = Depends(get_db),
):
    await _ensure_project(db, project_id)
    seen: set[str] = set()
    for it in items:
        if it.label in seen:
            raise HTTPException(status_code=400, detail=f"設備 label 重複：{it.label}")
        seen.add(it.label)

    await db.execute(delete(ProjectDevice).where(ProjectDevice.project_id == project_id))
    for it in items:
        db.add(ProjectDevice(
            project_id=project_id,
            label=it.label,
            platform=it.platform,
            platform_version=it.platform_version,
            device_name=it.device_name,
            avd_name=it.avd_name,
            udid=it.udid,
            automation_name=it.automation_name,
            extra_caps_json=it.extra_caps_json,
        ))
    await db.flush()

    rows = (await db.execute(
        select(ProjectDevice)
        .where(ProjectDevice.project_id == project_id)
        .order_by(ProjectDevice.label)
    )).scalars().all()
    return DevicesListResponse(project_id=project_id, items=rows)
