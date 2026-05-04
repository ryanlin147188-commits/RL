"""Mock Endpoint REST endpoints — DB 持久化(取代前端 localStorage)。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.project_membership import ensure_project_member
from app.database import get_db
from app.models.mock_endpoint import MockEndpoint
from app.models.user import User
from app.schemas.mock_endpoint import (
    MockEndpointCreate,
    MockEndpointResponse,
    MockEndpointUpdate,
)

router = APIRouter()


@router.get(
    "/mock-endpoints",
    response_model=list[MockEndpointResponse],
    tags=["Z · Mock 端點"],
    dependencies=[Depends(ensure_project_member)],
)
async def list_mock_endpoints(
    project_id: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(MockEndpoint).order_by(desc(MockEndpoint.created_at))
    if user.organization_id:
        stmt = stmt.where(MockEndpoint.organization_id == user.organization_id)
    if project_id:
        stmt = stmt.where(MockEndpoint.project_id == project_id)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.post(
    "/mock-endpoints",
    response_model=MockEndpointResponse,
    status_code=201,
    tags=["Z · Mock 端點"],
)
async def create_mock_endpoint(
    payload: MockEndpointCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not (payload.path or "").startswith("/"):
        raise HTTPException(400, "path must start with /")
    m = MockEndpoint(
        organization_id=user.organization_id,
        project_id=payload.project_id,
        name=payload.name,
        method=(payload.method or "GET").upper(),
        path=payload.path,
        description=payload.description,
        enabled=payload.enabled,
        status_code=payload.status_code,
        delay_ms=payload.delay_ms,
        response_headers_json=payload.response_headers_json,
        response_body_text=payload.response_body_text,
        request_headers_json=payload.request_headers_json,
        request_body_text=payload.request_body_text,
    )
    db.add(m)
    await db.flush()
    await db.refresh(m)
    return m


@router.get(
    "/mock-endpoints/{mock_id}",
    response_model=MockEndpointResponse,
    tags=["Z · Mock 端點"],
)
async def get_mock_endpoint(
    mock_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    m = await db.get(MockEndpoint, mock_id)
    if not m or (user.organization_id and m.organization_id != user.organization_id):
        raise HTTPException(404, "Mock endpoint not found")
    return m


@router.put(
    "/mock-endpoints/{mock_id}",
    response_model=MockEndpointResponse,
    tags=["Z · Mock 端點"],
)
async def update_mock_endpoint(
    mock_id: str,
    payload: MockEndpointUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    m = await db.get(MockEndpoint, mock_id)
    if not m or (user.organization_id and m.organization_id != user.organization_id):
        raise HTTPException(404, "Mock endpoint not found")
    data = payload.model_dump(exclude_unset=True)
    if "method" in data and data["method"]:
        data["method"] = data["method"].upper()
    if "path" in data and data["path"] and not data["path"].startswith("/"):
        raise HTTPException(400, "path must start with /")
    for key, val in data.items():
        setattr(m, key, val)
    await db.flush()
    await db.refresh(m)
    return m


@router.delete(
    "/mock-endpoints/{mock_id}",
    status_code=204,
    tags=["Z · Mock 端點"],
)
async def delete_mock_endpoint(
    mock_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    m = await db.get(MockEndpoint, mock_id)
    if not m or (user.organization_id and m.organization_id != user.organization_id):
        raise HTTPException(404, "Mock endpoint not found")
    await db.delete(m)
    await db.flush()
