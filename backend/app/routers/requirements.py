"""Requirement 需求 + RTM 追溯矩陣 REST endpoints。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.defect import Defect
from app.models.execution_report import ExecutionReport
from app.models.execution_step_log import ExecutionStepLog
from app.models.requirement import (
    Requirement,
    RequirementPriority,
    RequirementSource,
    RequirementStatus,
    RequirementTestcaseLink,
)
from app.models.tree_node import LevelType, TreeNode
from app.schemas.requirement import (
    RequirementCreate,
    RequirementResponse,
    RequirementUpdate,
    RtmCell,
    RtmLinkUpdate,
    RtmMatrixResponse,
)

router = APIRouter()


async def _next_code(db: AsyncSession, project_id: str) -> str:
    result = await db.execute(
        select(func.count(Requirement.id)).where(Requirement.project_id == project_id)
    )
    n = (result.scalar_one_or_none() or 0) + 1
    return f"REQ-{n:03d}"


def _resolve_enum(enum_cls, val, default):
    if val is None:
        return default
    try:
        return enum_cls(val)
    except ValueError:
        return default


async def _to_response(db: AsyncSession, r: Requirement) -> RequirementResponse:
    links = await db.execute(
        select(RequirementTestcaseLink.testcase_node_id).where(
            RequirementTestcaseLink.requirement_id == r.id
        )
    )
    return RequirementResponse(
        id=r.id,
        project_id=r.project_id,
        code=r.code,
        title=r.title,
        description=r.description,
        parent_id=r.parent_id,
        source=r.source.value if hasattr(r.source, "value") else str(r.source),
        priority=r.priority.value if hasattr(r.priority, "value") else str(r.priority),
        status=r.status.value if hasattr(r.status, "value") else str(r.status),
        owner=r.owner,
        created_at=r.created_at,
        updated_at=r.updated_at,
        linked_testcase_ids=[row[0] for row in links.all()],
    )


@router.get("/requirements", response_model=list[RequirementResponse], tags=["O · 需求 / RTM"])
async def list_requirements(
    project_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Requirement).order_by(Requirement.code)
    if project_id:
        stmt = stmt.where(Requirement.project_id == project_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [await _to_response(db, r) for r in rows]


@router.post("/requirements", response_model=RequirementResponse, status_code=201, tags=["O · 需求 / RTM"])
async def create_requirement(payload: RequirementCreate, db: AsyncSession = Depends(get_db)):
    code = payload.code or await _next_code(db, payload.project_id)
    r = Requirement(
        project_id=payload.project_id,
        code=code,
        parent_id=payload.parent_id,
        title=payload.title,
        description=payload.description,
        source=_resolve_enum(RequirementSource, payload.source, RequirementSource.PRD),
        priority=_resolve_enum(RequirementPriority, payload.priority, RequirementPriority.SHOULD),
        status=_resolve_enum(RequirementStatus, payload.status, RequirementStatus.DRAFT),
        owner=payload.owner,
    )
    db.add(r)
    await db.flush()
    await db.refresh(r)
    return await _to_response(db, r)


@router.get("/requirements/{req_id}", response_model=RequirementResponse, tags=["O · 需求 / RTM"])
async def get_requirement(req_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.get(Requirement, req_id)
    if not r:
        raise HTTPException(404, "Requirement not found")
    return await _to_response(db, r)


@router.put("/requirements/{req_id}", response_model=RequirementResponse, tags=["O · 需求 / RTM"])
async def update_requirement(
    req_id: str, payload: RequirementUpdate, db: AsyncSession = Depends(get_db)
):
    r = await db.get(Requirement, req_id)
    if not r:
        raise HTTPException(404, "Requirement not found")
    data = payload.model_dump(exclude_unset=True)
    for key, val in data.items():
        if key == "source" and val is not None:
            r.source = _resolve_enum(RequirementSource, val, r.source)
        elif key == "priority" and val is not None:
            r.priority = _resolve_enum(RequirementPriority, val, r.priority)
        elif key == "status" and val is not None:
            r.status = _resolve_enum(RequirementStatus, val, r.status)
        else:
            setattr(r, key, val)
    await db.flush()
    await db.refresh(r)
    return await _to_response(db, r)


@router.delete("/requirements/{req_id}", status_code=204, tags=["O · 需求 / RTM"])
async def delete_requirement(req_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.get(Requirement, req_id)
    if not r:
        raise HTTPException(404, "Requirement not found")
    await db.delete(r)
    await db.flush()


# ========== RTM 關聯 + 矩陣 ==========

@router.put("/requirements/{req_id}/links", tags=["O · 需求 / RTM"])
async def replace_requirement_links(
    req_id: str, payload: RtmLinkUpdate, db: AsyncSession = Depends(get_db)
):
    """整批替換需求 ↔ 測試案例 的關聯。"""
    r = await db.get(Requirement, req_id)
    if not r:
        raise HTTPException(404, "Requirement not found")
    # 清空舊關聯
    await db.execute(
        select(RequirementTestcaseLink).where(RequirementTestcaseLink.requirement_id == req_id)
    )
    from sqlalchemy import delete as sql_delete
    await db.execute(
        sql_delete(RequirementTestcaseLink).where(RequirementTestcaseLink.requirement_id == req_id)
    )
    # 寫入新關聯
    for tc_id in payload.testcase_node_ids:
        if not tc_id:
            continue
        db.add(RequirementTestcaseLink(requirement_id=req_id, testcase_node_id=tc_id))
    await db.flush()
    return {"ok": True, "count": len(payload.testcase_node_ids)}


@router.get("/rtm/matrix", response_model=RtmMatrixResponse, tags=["O · 需求 / RTM"])
async def rtm_matrix(
    project_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """RTM 矩陣：requirements × testcases × 最近執行狀態。"""
    # 1) 該專案所有需求
    reqs_rows = (await db.execute(
        select(Requirement).where(Requirement.project_id == project_id).order_by(Requirement.code)
    )).scalars().all()
    requirements = [await _to_response(db, r) for r in reqs_rows]

    # 2) 該專案所有 TESTCASE 節點
    tcs_rows = (await db.execute(
        select(TreeNode).where(
            TreeNode.project_id == project_id, TreeNode.level_type == LevelType.TESTCASE
        ).order_by(TreeNode.name)
    )).scalars().all()
    testcases = [{"id": t.id, "title": t.name} for t in tcs_rows]

    # 3) 所有 link（限本專案需求）
    req_ids = [r.id for r in reqs_rows]
    cells: list[RtmCell] = []
    if req_ids:
        links = (await db.execute(
            select(RequirementTestcaseLink).where(
                RequirementTestcaseLink.requirement_id.in_(req_ids)
            )
        )).scalars().all()
        # 4) 每對 (req, tc) 算最近一次執行的 step status
        # 取每個 testcase 最新一筆 step 紀錄做為 latest status
        # (簡化：取最新 ExecutionStepLog by testcase_node_id)
        if links:
            tc_ids = list({l.testcase_node_id for l in links})
            # 對每個 tc 抓最近一個 step（按 created_at 倒序）
            # 為了避免 N+1，這裡做 distinct on：取每 testcase_node_id 一筆最新
            latest_status: dict[str, str] = {}
            from sqlalchemy import desc as sql_desc
            for tc_id in tc_ids:
                row = (await db.execute(
                    select(ExecutionStepLog.status)
                    .where(ExecutionStepLog.testcase_node_id == tc_id)
                    .order_by(sql_desc(ExecutionStepLog.id))
                    .limit(1)
                )).first()
                if row:
                    s = row[0]
                    latest_status[tc_id] = s.value if hasattr(s, "value") else str(s)
            for l in links:
                cells.append(RtmCell(
                    requirement_id=l.requirement_id,
                    testcase_node_id=l.testcase_node_id,
                    last_status=latest_status.get(l.testcase_node_id),
                ))

    return RtmMatrixResponse(
        requirements=requirements,
        testcases=testcases,
        cells=cells,
    )


# ========== RTM 追溯鏈（User Story → AC → Test Case → Bug） ==========

@router.get("/rtm/chain", tags=["O · 需求 / RTM"])
async def rtm_chain(
    project_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """完整追溯鏈：User Story → AC → Test Case → Bug。

    對應關係（無需新增 schema，直接套用既有結構）：
    - 頂層需求（parent_id IS NULL） → User Story
    - 子需求（parent_id 指向 User Story）→ Acceptance Criteria (AC)
    - AC 透過 RequirementTestcaseLink 關聯到 TreeNode (TESTCASE) → Test Case
    - Defect.linked_testcase_id 指向 TreeNode → Bug

    回傳：
    {
        "user_stories": [ { story, acs: [ { ac, test_cases: [ { tc, last_status, defects: [...] } ] } ] } ],
        "summary": { user_stories, acs, test_cases, defects, orphan_defects }
    }
    """
    from sqlalchemy import desc as sql_desc

    # 1) 該專案所有需求
    reqs = (await db.execute(
        select(Requirement)
        .where(Requirement.project_id == project_id)
        .order_by(Requirement.code)
    )).scalars().all()
    req_by_id = {r.id: r for r in reqs}

    # 2) 所有 link（需求 ↔ 測試案例）
    req_ids = [r.id for r in reqs]
    links = []
    if req_ids:
        links = (await db.execute(
            select(RequirementTestcaseLink).where(
                RequirementTestcaseLink.requirement_id.in_(req_ids)
            )
        )).scalars().all()
    tc_ids = list({l.testcase_node_id for l in links})

    # 3) Test case nodes
    tcs_by_id: dict[str, TreeNode] = {}
    if tc_ids:
        tc_rows = (await db.execute(
            select(TreeNode).where(TreeNode.id.in_(tc_ids))
        )).scalars().all()
        tcs_by_id = {t.id: t for t in tc_rows}

    # 4) 每個 testcase 的最新執行狀態
    latest_status: dict[str, str] = {}
    for tc_id in tc_ids:
        row = (await db.execute(
            select(ExecutionStepLog.status)
            .where(ExecutionStepLog.testcase_node_id == tc_id)
            .order_by(sql_desc(ExecutionStepLog.id))
            .limit(1)
        )).first()
        if row:
            s = row[0]
            latest_status[tc_id] = s.value if hasattr(s, "value") else str(s)

    # 5) 該專案所有 defect
    defects = (await db.execute(
        select(Defect).where(Defect.project_id == project_id).order_by(Defect.code)
    )).scalars().all()
    defects_by_tc: dict[str, list[Defect]] = {}
    orphan_defects: list[Defect] = []
    for d in defects:
        if d.linked_testcase_id and d.linked_testcase_id in tcs_by_id:
            defects_by_tc.setdefault(d.linked_testcase_id, []).append(d)
        else:
            orphan_defects.append(d)

    # 6) requirement → linked testcase ids
    tcs_per_req: dict[str, list[str]] = {}
    for l in links:
        tcs_per_req.setdefault(l.requirement_id, []).append(l.testcase_node_id)

    def _enum_str(v):
        return v.value if hasattr(v, "value") else str(v) if v is not None else None

    def _serialize_tc(tc_id: str) -> dict:
        tc = tcs_by_id.get(tc_id)
        if not tc:
            return {"id": tc_id, "title": "(已刪除的案例)", "last_status": None, "defects": []}
        return {
            "id": tc.id,
            "title": tc.name,
            "last_status": latest_status.get(tc_id),
            "defects": [
                {
                    "id": d.id,
                    "code": d.code,
                    "title": d.title,
                    "status": _enum_str(d.status),
                    "severity": _enum_str(d.severity),
                    "priority": _enum_str(d.priority),
                    "assignee": d.assignee,
                }
                for d in defects_by_tc.get(tc_id, [])
            ],
        }

    def _serialize_req(r: Requirement, depth: int = 0) -> dict:
        children = [
            _serialize_req(c, depth + 1)
            for c in reqs
            if c.parent_id == r.id
        ]
        return {
            "id": r.id,
            "code": r.code,
            "title": r.title,
            "parent_id": r.parent_id,
            "source": _enum_str(r.source),
            "priority": _enum_str(r.priority),
            "status": _enum_str(r.status),
            "owner": r.owner,
            "test_cases": [_serialize_tc(tc_id) for tc_id in tcs_per_req.get(r.id, [])],
            "acs" if depth == 0 else "children": children,
        }

    user_stories = [_serialize_req(r, 0) for r in reqs if r.parent_id is None]

    summary = {
        "user_stories": len(user_stories),
        "acs": len([r for r in reqs if r.parent_id is not None]),
        "test_cases_in_chain": len(tc_ids),
        "defects_total": len(defects),
        "defects_in_chain": sum(len(v) for v in defects_by_tc.values()),
        "orphan_defects": len(orphan_defects),
    }

    return {
        "user_stories": user_stories,
        "summary": summary,
        "orphan_defects": [
            {
                "id": d.id,
                "code": d.code,
                "title": d.title,
                "status": _enum_str(d.status),
                "severity": _enum_str(d.severity),
            }
            for d in orphan_defects
        ],
    }
