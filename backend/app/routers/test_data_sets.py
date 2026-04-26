"""Test Data Set (DDT) REST endpoints。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import Pagination
from app.database import get_db
from app.models.test_data_set import DataSetCategory, TestDataSet
from app.schemas.test_data_set import (
    TestDataSetCreate,
    TestDataSetResponse,
    TestDataSetUpdate,
)

router = APIRouter()


async def _next_code(db: AsyncSession, project_id: str) -> str:
    result = await db.execute(
        select(func.count(TestDataSet.id)).where(TestDataSet.project_id == project_id)
    )
    n = (result.scalar_one_or_none() or 0) + 1
    return f"DS-{n:03d}"


def _resolve_category(val, default):
    if val is None:
        return default
    try:
        return DataSetCategory(val)
    except ValueError:
        return default


@router.get("/data-sets", response_model=list[TestDataSetResponse], tags=["P · 測試資料集 (DDT)"])
async def list_data_sets(
    project_id: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    page: Pagination = Depends(Pagination.from_query),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(TestDataSet).order_by(desc(TestDataSet.created_at))
    if project_id:
        stmt = stmt.where(TestDataSet.project_id == project_id)
    if category:
        stmt = stmt.where(TestDataSet.category == DataSetCategory(category))
    stmt = page.apply(stmt)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.post(
    "/data-sets",
    response_model=TestDataSetResponse,
    status_code=201,
    tags=["P · 測試資料集 (DDT)"],
)
async def create_data_set(payload: TestDataSetCreate, db: AsyncSession = Depends(get_db)):
    code = payload.code or await _next_code(db, payload.project_id)
    ds = TestDataSet(
        project_id=payload.project_id,
        code=code,
        name=payload.name,
        description=payload.description,
        category=_resolve_category(payload.category, DataSetCategory.OTHER),
        columns_json=payload.columns_json or [],
        rows_json=payload.rows_json or [],
        linked_testcase_ids=payload.linked_testcase_ids,
        owner=payload.owner,
    )
    db.add(ds)
    await db.flush()
    await db.refresh(ds)
    return ds


@router.get(
    "/data-sets/{ds_id}",
    response_model=TestDataSetResponse,
    tags=["P · 測試資料集 (DDT)"],
)
async def get_data_set(ds_id: str, db: AsyncSession = Depends(get_db)):
    ds = await db.get(TestDataSet, ds_id)
    if not ds:
        raise HTTPException(404, "Data set not found")
    return ds


@router.put(
    "/data-sets/{ds_id}",
    response_model=TestDataSetResponse,
    tags=["P · 測試資料集 (DDT)"],
)
async def update_data_set(
    ds_id: str, payload: TestDataSetUpdate, db: AsyncSession = Depends(get_db)
):
    ds = await db.get(TestDataSet, ds_id)
    if not ds:
        raise HTTPException(404, "Data set not found")
    data = payload.model_dump(exclude_unset=True)
    for key, val in data.items():
        if key == "category" and val is not None:
            ds.category = _resolve_category(val, ds.category)
        else:
            setattr(ds, key, val)
    await db.flush()
    await db.refresh(ds)
    return ds


@router.delete(
    "/data-sets/{ds_id}", status_code=204, tags=["P · 測試資料集 (DDT)"]
)
async def delete_data_set(ds_id: str, db: AsyncSession = Depends(get_db)):
    ds = await db.get(TestDataSet, ds_id)
    if not ds:
        raise HTTPException(404, "Data set not found")
    await db.delete(ds)
    await db.flush()
