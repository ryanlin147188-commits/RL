"""收集執行計畫(展開 + 前置案例 + 去重 + cycle 偵測)。

被 `routers/executions.py` 與 `routers/local_runner.py` 共用,讓 docker /
local 兩條執行路徑看到同一份「先跑 setup,再跑 main」的計畫。

主要流程:
  1. 每個 input node_id 用 `collect_testcase_ids` 展開為 leaf testcase
  2. 合併、去重 → main_testcase_ids
  3. 對每個 main 查 enabled 的前置(testcase_precondition_links),依 sort_order
     遞迴展開(前置自己也可以有前置),BFS 順序
  4. DFS cycle 偵測:任何 a → ... → a 一律拒絕
  5. 跨 project 一律拒絕(避免報告 project_id 歧義)
  6. 權限檢查:逐一 ensure_project_in_scope

回傳:
    {
        "project_id": str,
        "setup_testcase_ids": list[str],   # 已去重、已 topological-ish 排序
        "main_testcase_ids": list[str],    # 維持輸入順序、去重
    }
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.scope import ensure_project_in_scope
from app.models.testcase_precondition_link import TestcasePreconditionLink
from app.models.tree_node import LevelType, TreeNode
from app.models.user import User
from app.services.execution_service import collect_testcase_ids


async def _expand_inputs(db: AsyncSession, node_ids: list[str]) -> list[str]:
    """把任意 node_id(可能是 folder / scenario 容器或葉子)逐一展開為
    leaf testcase id 列表;維持輸入順序,去重。"""
    seen: set[str] = set()
    out: list[str] = []
    for nid in node_ids:
        ids = await collect_testcase_ids(db, nid)
        for tid in ids:
            if tid not in seen:
                seen.add(tid)
                out.append(tid)
    return out


async def _gather_preconditions(
    db: AsyncSession, main_ids: list[str]
) -> list[str]:
    """BFS 展開 main 的前置案例(含巢狀前置),回傳「依首次出現順序去重後」
    的列表;同時偵測 cycle(any → ... → self)。

    cycle 規則:對每個 root,DFS 路徑上若再次遇到同一個 testcase id 就 raise。
    """
    if not main_ids:
        return []

    # 一次撈光所有可能用到的前置邊;省 N+1
    edges: dict[str, list[tuple[str, int]]] = {}
    rows = (
        await db.execute(
            select(
                TestcasePreconditionLink.testcase_id,
                TestcasePreconditionLink.precondition_testcase_id,
                TestcasePreconditionLink.sort_order,
            ).where(TestcasePreconditionLink.enabled.is_(True))
        )
    ).all()
    for tc_id, pre_id, order in rows:
        edges.setdefault(tc_id, []).append((pre_id, order))
    for tc_id in edges:
        edges[tc_id].sort(key=lambda x: (x[1], x[0]))

    visited_global: set[str] = set()
    ordered: list[str] = []

    def _dfs(node: str, on_path: set[str]) -> None:
        on_path.add(node)
        for pre_id, _ in edges.get(node, []):
            if pre_id in on_path:
                raise HTTPException(
                    status_code=400,
                    detail=f"前置案例形成循環:{pre_id}",
                )
            _dfs(pre_id, on_path)
            if pre_id not in visited_global:
                visited_global.add(pre_id)
                ordered.append(pre_id)
        on_path.remove(node)

    for main_id in main_ids:
        _dfs(main_id, set())

    return ordered


async def collect_execution_plan(
    db: AsyncSession,
    *,
    node_ids: list[str],
    user: User,
) -> dict[str, Any]:
    if not node_ids:
        raise HTTPException(status_code=400, detail="node_ids 不可為空")

    # 1) 展開到 leaf testcase
    main_ids = await _expand_inputs(db, node_ids)
    if not main_ids:
        raise HTTPException(
            status_code=400,
            detail="No TESTCASE nodes found under the given node(s)",
        )

    # 2) 收集所有相關 testcase 的 project_id(主+前置都要),確保跨 project 不混跑
    all_candidates: set[str] = set(main_ids)
    pre_ids_preview = await _gather_preconditions(db, main_ids)
    all_candidates.update(pre_ids_preview)

    rows = (
        await db.execute(
            select(TreeNode.id, TreeNode.project_id, TreeNode.level_type).where(
                TreeNode.id.in_(all_candidates)
            )
        )
    ).all()
    type_map: dict[str, LevelType] = {}
    project_map: dict[str, str] = {}
    for tid, pid, ltype in rows:
        project_map[tid] = pid
        type_map[tid] = ltype

    # leaf 必須是 TESTCASE(避免 precondition 指到 folder)
    bad_leaf = [
        tid for tid in main_ids + pre_ids_preview
        if type_map.get(tid) != LevelType.TESTCASE
    ]
    if bad_leaf:
        raise HTTPException(
            status_code=400,
            detail=f"以下節點不是 TESTCASE: {bad_leaf[:5]}",
        )

    project_ids = {project_map.get(tid) for tid in main_ids if project_map.get(tid)}
    if len(project_ids) != 1:
        raise HTTPException(
            status_code=400,
            detail="跨 project 批次執行不支援,請每次只選同一個 project 的測試案例",
        )
    project_id = next(iter(project_ids))

    # 前置如果跨 project 也直接擋(避免 setup 來自其他 project)
    pre_project_ids = {
        project_map.get(tid) for tid in pre_ids_preview if project_map.get(tid)
    }
    if pre_project_ids and pre_project_ids != {project_id}:
        raise HTTPException(
            status_code=400,
            detail="前置案例必須與主案例屬於同一個 project",
        )

    # 3) 權限檢查(以 project_id 為單位即可,不必逐 testcase)
    await ensure_project_in_scope(db, project_id, user, not_found_detail="Project not found")

    # 4) 從 main 集合中扣掉「同時出現在 setup 內」的 id —— main 已含則不重複跑
    main_set = set(main_ids)
    setup_ids = [tid for tid in pre_ids_preview if tid not in main_set]

    return {
        "project_id": project_id,
        "setup_testcase_ids": setup_ids,
        "main_testcase_ids": main_ids,
    }
