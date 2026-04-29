"""Test Document REST endpoints。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.scope import (
    ensure_project_in_scope,
    ensure_project_writable,
    scope_by_project,
)
from app.common import Pagination
from app.database import get_db
from app.models.test_document import DocumentCategory, TestDocument
from app.models.user import User
from app.schemas.test_document import (
    TestDocumentCreate,
    TestDocumentResponse,
    TestDocumentUpdate,
)

router = APIRouter()


async def _next_code(db: AsyncSession, project_id: str) -> str:
    result = await db.execute(
        select(func.count(TestDocument.id)).where(TestDocument.project_id == project_id)
    )
    n = (result.scalar_one_or_none() or 0) + 1
    return f"DOC-{n:03d}"


def _resolve_category(val, default):
    if val is None:
        return default
    try:
        return DocumentCategory(val)
    except ValueError:
        return default


@router.get(
    "/documents",
    response_model=list[TestDocumentResponse],
    tags=["Q · 測試文件"],
)
async def list_documents(
    project_id: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    page: Pagination = Depends(Pagination.from_query),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(TestDocument).order_by(desc(TestDocument.updated_at))
    if project_id:
        stmt = stmt.where(TestDocument.project_id == project_id)
    if category:
        stmt = stmt.where(TestDocument.category == DocumentCategory(category))
    stmt = scope_by_project(stmt, TestDocument, user)
    stmt = page.apply(stmt)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.post(
    "/documents",
    response_model=TestDocumentResponse,
    status_code=201,
    tags=["Q · 測試文件"],
)
async def create_document(
    payload: TestDocumentCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await ensure_project_writable(db, payload.project_id, user)
    code = payload.code or await _next_code(db, payload.project_id)
    doc = TestDocument(
        project_id=payload.project_id,
        code=code,
        title=payload.title,
        category=_resolve_category(payload.category, DocumentCategory.NOTE),
        content_md=payload.content_md,
        summary=payload.summary,
        owner=payload.owner,
        tags=payload.tags,
    )
    db.add(doc)
    await db.flush()
    await db.refresh(doc)
    return doc


@router.get(
    "/documents/{doc_id}",
    response_model=TestDocumentResponse,
    tags=["Q · 測試文件"],
)
async def get_document(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await db.get(TestDocument, doc_id)
    await ensure_project_in_scope(
        db, doc.project_id if doc else None, user, not_found_detail="Document not found"
    )
    return doc


@router.put(
    "/documents/{doc_id}",
    response_model=TestDocumentResponse,
    tags=["Q · 測試文件"],
)
async def update_document(
    doc_id: str,
    payload: TestDocumentUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await db.get(TestDocument, doc_id)
    await ensure_project_in_scope(
        db, doc.project_id if doc else None, user, not_found_detail="Document not found"
    )
    data = payload.model_dump(exclude_unset=True)
    for key, val in data.items():
        if key == "category" and val is not None:
            doc.category = _resolve_category(val, doc.category)
        else:
            setattr(doc, key, val)
    await db.flush()
    await db.refresh(doc)
    return doc


@router.delete(
    "/documents/{doc_id}",
    status_code=204,
    tags=["Q · 測試文件"],
)
async def delete_document(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await db.get(TestDocument, doc_id)
    await ensure_project_in_scope(
        db, doc.project_id if doc else None, user, not_found_detail="Document not found"
    )
    await db.delete(doc)
    await db.flush()
