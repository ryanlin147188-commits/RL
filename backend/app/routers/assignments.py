"""Generic assignment endpoints (Phase 2).

Routes:
    POST   /api/assignments        — create / update assignment
    DELETE /api/assignments        — clear (params: entity_type, entity_id)
    GET    /api/assignments/me     — list things assigned to current user

Why one router instead of five (per-entity)?
    The five reviewable entities all mix in `Assignable` from
    app.auth.tenant. The schema is identical, so write the route once,
    dispatch to the correct model with a small lookup table.

Reuses:
    * /api/auth/users/assignable -- already loads org users
    * /api/settings/groups       -- already loads groups + members
    * notification_dispatch.notify -- pushes "assignment.received" to
      the assignee's inbox AND emails them when their preference says so

The lock semantics from RFC-Review-1 do NOT apply here -- assignment is
metadata, not content; an approved entity can still be reassigned.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.assignment import (
    AssignableEntityType,
    AssignmentResponse,
    AssignRequest,
)
from app.services.notification_dispatch import notify

router = APIRouter()


# ── Entity dispatch table ────────────────────────────────────────────────

def _model_for(entity_type: AssignableEntityType):
    """Look up the SQLAlchemy model that holds the assignment columns for
    a given entity_type. Imported lazily to avoid circular imports."""
    from app.models.defect import Defect
    from app.models.requirement import Requirement
    from app.models.review import ReviewRecord
    from app.models.test_document import TestDocument
    from app.models.tree_node import LevelType, TreeNode

    return {
        AssignableEntityType.REVIEW: (ReviewRecord, None),
        AssignableEntityType.DEFECT: (Defect, None),
        AssignableEntityType.TESTCASE: (TreeNode, LevelType.TESTCASE),
        AssignableEntityType.REQUIREMENT: (Requirement, None),
        AssignableEntityType.DOCUMENT: (TestDocument, None),
    }[entity_type]


def _human_label(entity_type: AssignableEntityType, obj) -> str:
    """Render a short label for the notification title.

    Each entity has a different "name" attribute -- pick whichever is
    most useful to surface in the bell."""
    if entity_type == AssignableEntityType.REVIEW:
        return f"{obj.entity_type.value} {obj.entity_id[:8]}"
    if entity_type in (AssignableEntityType.TESTCASE, AssignableEntityType.REQUIREMENT):
        return getattr(obj, "name", obj.id)
    if entity_type == AssignableEntityType.DOCUMENT:
        return getattr(obj, "title", obj.id)
    if entity_type == AssignableEntityType.DEFECT:
        return f"{obj.code} {obj.title[:40]}" if obj.title else obj.code
    return obj.id


async def _load_for_assignment(
    db: AsyncSession,
    entity_type: AssignableEntityType,
    entity_id: str,
    user: User,
):
    """Resolve obj + scope-check it. 404 if not in user's org / wrong type."""
    Model, level_filter = _model_for(entity_type)
    obj = await db.get(Model, entity_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{entity_type.value} not found")
    # Tenant check
    obj_org = getattr(obj, "organization_id", None)
    if not user.is_superuser and obj_org and obj_org != user.organization_id:
        raise HTTPException(status_code=404, detail=f"{entity_type.value} not found")
    # TESTCASE-level filter for TreeNode
    if level_filter is not None and getattr(obj, "level_type", None) != level_filter:
        raise HTTPException(
            status_code=400,
            detail="only TESTCASE nodes are assignable; others are organisational containers",
        )
    return obj


# ── Endpoints ────────────────────────────────────────────────────────────

@router.post(
    "/assignments",
    response_model=AssignmentResponse,
    status_code=200,
    tags=["AC · 指派"],
)
async def assign(
    payload: AssignRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Set or replace the assignee on an entity. Notifies the assignee."""
    obj = await _load_for_assignment(db, payload.entity_type, payload.entity_id, user)

    obj.assigned_to = payload.assignee
    obj.assigned_to_type = payload.assignee_type.value
    obj.assigned_by = user.username
    obj.assigned_at = datetime.utcnow()
    await db.flush()
    await db.refresh(obj)

    # Push notification + email (when subscriber has email enabled).
    # Notify everyone affected: for user-type assignment, just the assignee;
    # for group-type, expand to members. Group expansion intentionally
    # deferred to v1.2 (TodoItem already has it; share that helper later).
    label = _human_label(payload.entity_type, obj)
    if payload.assignee_type == "user" or payload.assignee_type.value == "user":
        await notify(
            db=db,
            event_key="assignment.received",
            recipient=payload.assignee,
            title=f"您被指派一筆 {payload.entity_type.value}",
            body=f"「{label}」由 {user.username} 指派給您。",
            level="info",
            link=None,
            related_entity_type=payload.entity_type.value,
            related_entity_id=payload.entity_id,
            organization_id=getattr(obj, "organization_id", None),
        )

    return AssignmentResponse(
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        assigned_to=obj.assigned_to,
        assigned_to_type=obj.assigned_to_type,
        assigned_by=obj.assigned_by,
        assigned_at=obj.assigned_at,
    )


@router.delete("/assignments", status_code=204, tags=["AC · 指派"])
async def unassign(
    entity_type: AssignableEntityType = Query(...),
    entity_id: str = Query(..., min_length=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Clear the assignee on an entity. No notification (silent)."""
    obj = await _load_for_assignment(db, entity_type, entity_id, user)
    obj.assigned_to = None
    obj.assigned_to_type = "user"  # reset to default
    obj.assigned_by = None
    obj.assigned_at = None
    await db.flush()


@router.get(
    "/assignments/me",
    response_model=List[AssignmentResponse],
    tags=["AC · 指派"],
)
async def list_my_assignments(
    entity_type: Optional[AssignableEntityType] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return rows where assigned_to == current user, optionally filtered
    to a single entity_type."""
    types = [entity_type] if entity_type else list(AssignableEntityType)
    out: List[AssignmentResponse] = []
    for et in types:
        Model, level_filter = _model_for(et)
        stmt = select(Model).where(Model.assigned_to == user.username)
        if not user.is_superuser:
            stmt = stmt.where(Model.organization_id == user.organization_id)
        if level_filter is not None:
            stmt = stmt.where(Model.level_type == level_filter)
        rows = (await db.execute(stmt)).scalars().all()
        for r in rows:
            out.append(
                AssignmentResponse(
                    entity_type=et,
                    entity_id=r.id if et != AssignableEntityType.REVIEW else r.entity_id,
                    assigned_to=r.assigned_to,
                    assigned_to_type=r.assigned_to_type,
                    assigned_by=r.assigned_by,
                    assigned_at=r.assigned_at,
                )
            )
    return out
