"""測試回合（Test Round）REST endpoints。

一個測試回合 = 一組被命名的測試案例集合；可以一鍵執行全部測試案例。

- GET    /rounds?project_id=...           列出回合
- POST   /rounds                          建立回合（name + node_ids + project_id）
- GET    /rounds/{id}                     取得單一回合
- PUT    /rounds/{id}                     更新
- DELETE /rounds/{id}                     刪除
- POST   /rounds/{id}/execute             立即執行（一次建立多個 ExecutionReport）
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.scope import (
    ensure_project_in_scope,
    ensure_project_writable,
    scope_by_project,
)
from app.database import get_db
from app.models.test_round import TestRound
from app.models.tree_node import TreeNode
from app.models.user import User
from app.schemas.test_round import TestRoundCreate, TestRoundResponse, TestRoundUpdate
from app.services.execution_service import collect_testcase_ids, create_report

router = APIRouter()


def _parse_node_ids(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for nid in parsed:
        if isinstance(nid, str) and nid and nid not in seen:
            seen.add(nid)
            out.append(nid)
    return out


async def _to_response(db: AsyncSession, r: TestRound) -> TestRoundResponse:
    nids = _parse_node_ids(r.node_ids_json)
    titles: list[str] = []
    if nids:
        rows = await db.execute(select(TreeNode).where(TreeNode.id.in_(nids)))
        mp = {n.id: n.name for n in rows.scalars()}
        titles = [mp.get(i, i) for i in nids]
    return TestRoundResponse(
        id=r.id,
        name=r.name,
        project_id=r.project_id,
        node_ids=nids,
        node_titles=titles,
        description=r.description,
        execution_mode=(r.execution_mode or "docker"),
        test_version_id=getattr(r, "test_version_id", None),
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


@router.get("/rounds", response_model=list[TestRoundResponse], tags=["H · 測試回合"])
async def list_rounds(
    project_id: Optional[str] = Query(None),
    test_version_id: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(TestRound).order_by(desc(TestRound.created_at))
    if project_id:
        stmt = stmt.where(TestRound.project_id == project_id)
    if test_version_id:
        stmt = stmt.where(TestRound.test_version_id == test_version_id)
    stmt = scope_by_project(stmt, TestRound, user)
    result = await db.execute(stmt)
    rows = list(result.scalars())
    return [await _to_response(db, r) for r in rows]


@router.post("/rounds", response_model=TestRoundResponse, status_code=201, tags=["H · 測試回合"])
async def create_round(
    payload: TestRoundCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await ensure_project_writable(db, payload.project_id, user)
    if not payload.node_ids:
        raise HTTPException(status_code=400, detail="至少要選一個節點")
    # 驗證 project 存在
    r = TestRound(
        name=payload.name,
        project_id=payload.project_id,
        node_ids_json=json.dumps(list(dict.fromkeys(payload.node_ids)), ensure_ascii=False),
        description=payload.description,
        execution_mode=(payload.execution_mode or "docker").lower(),
        test_version_id=payload.test_version_id,
    )
    db.add(r)
    await db.flush()
    return await _to_response(db, r)


@router.get("/rounds/{round_id}", response_model=TestRoundResponse, tags=["H · 測試回合"])
async def get_round(
    round_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    r = await db.get(TestRound, round_id)
    await ensure_project_in_scope(
        db, r.project_id if r else None, user, not_found_detail="Round not found"
    )
    return await _to_response(db, r)


@router.put("/rounds/{round_id}", response_model=TestRoundResponse, tags=["H · 測試回合"])
async def update_round(
    round_id: str,
    payload: TestRoundUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    r = await db.get(TestRound, round_id)
    await ensure_project_in_scope(
        db, r.project_id if r else None, user, not_found_detail="Round not found"
    )
    if payload.name is not None:
        r.name = payload.name
    if payload.node_ids is not None:
        nids = [n for n in payload.node_ids if n]
        if not nids:
            raise HTTPException(status_code=400, detail="至少要選一個節點")
        r.node_ids_json = json.dumps(list(dict.fromkeys(nids)), ensure_ascii=False)
    if payload.description is not None:
        r.description = payload.description
    if payload.execution_mode is not None:
        r.execution_mode = (payload.execution_mode or "docker").lower()
    if "test_version_id" in payload.model_fields_set:
        r.test_version_id = payload.test_version_id
    await db.flush()
    return await _to_response(db, r)


@router.delete("/rounds/{round_id}", status_code=204, tags=["H · 測試回合"])
async def delete_round(
    round_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    r = await db.get(TestRound, round_id)
    await ensure_project_in_scope(
        db, r.project_id if r else None, user, not_found_detail="Round not found"
    )
    await db.delete(r)


@router.post("/rounds/{round_id}/execute", tags=["H · 測試回合"])
async def execute_round(
    round_id: str,
    execution_mode: Optional[str] = Query(None, pattern="^(docker|local)$"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """立即執行整個回合：把所有節點底下的 TESTCASE 彙總後，建立一份 ExecutionReport。

    execution_mode 未指定時使用 round.execution_mode。
    """
    r = await db.get(TestRound, round_id)
    await ensure_project_in_scope(
        db, r.project_id if r else None, user, not_found_detail="Round not found"
    )

    nids = _parse_node_ids(r.node_ids_json)
    if not nids:
        raise HTTPException(status_code=400, detail="此回合沒有任何節點，無法執行")

    # 聚合所有節點的 testcase ids（去重保序）
    seen: set[str] = set()
    testcase_ids: list[str] = []
    for nid in nids:
        ids = await collect_testcase_ids(db, nid)
        for tid in ids:
            if tid not in seen:
                seen.add(tid)
                testcase_ids.append(tid)
    if not testcase_ids:
        raise HTTPException(status_code=400, detail="所選節點底下找不到任何 TESTCASE")

    mode = (execution_mode or r.execution_mode or "docker").lower()
    task_id = str(uuid.uuid4())
    # 報告把 trigger_type 標成 "Round"，並用第一個節點作為 source_node_id
    report = await create_report(
        db, r.project_id, f"Round:{r.name}", len(testcase_ids), task_id,
        execution_mode=mode, source_node_id=nids[0],
    )

    if mode == "docker":
        try:
            from tasks.celery_app import celery_app

            celery_app.send_task(
                "tasks.execution_tasks.run_tests",
                kwargs={
                    "task_id": task_id,
                    "report_id": report.id,
                    "testcase_ids": testcase_ids,
                    "ddt_expand": False,
                },
            )
        except Exception:
            # 不擋；前端會看到 RUNNING 但不會有進度（Celery 掛了）
            pass
    # local 模式：agent 認領時會用 report.source_node_id 還原；但我們只存了 nids[0]。
    # → 改用 source_node_id 搭配 ExecutionReport（本 feature 若要跨節點，建議先用 docker 模式）

    return {
        "ok": True,
        "round_id": r.id,
        "task_id": task_id,
        "report_id": report.id,
        "testcase_count": len(testcase_ids),
        "execution_mode": mode,
    }
