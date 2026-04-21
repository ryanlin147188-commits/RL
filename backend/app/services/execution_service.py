import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_report import ExecutionReport
from app.models.tree_node import LevelType, TreeNode


async def collect_testcase_ids(db: AsyncSession, node_id: str) -> list[str]:
    """
    收集指定節點（含其所有子孫節點）下的全部 TESTCASE 節點 ID。
    使用單次查詢 + Python 端遞迴，避免多次 DB 往返。
    """
    # 一次撈出同 project 下全部節點
    node_result = await db.execute(select(TreeNode).where(TreeNode.id == node_id))
    root = node_result.scalar_one_or_none()
    if root is None:
        return []

    all_nodes_result = await db.execute(
        select(TreeNode).where(TreeNode.project_id == root.project_id)
    )
    all_nodes = all_nodes_result.scalars().all()

    # 建立 id -> node 映射
    node_map: dict[str, TreeNode] = {n.id: n for n in all_nodes}

    # 建立 parent_id -> children 映射
    children_map: dict[str | None, list[str]] = {}
    for n in all_nodes:
        children_map.setdefault(n.parent_id, []).append(n.id)

    testcase_ids: list[str] = []

    def _walk(nid: str) -> None:
        node = node_map.get(nid)
        if node is None:
            return
        if node.level_type == LevelType.TESTCASE:
            testcase_ids.append(nid)
        else:
            for child_id in children_map.get(nid, []):
                _walk(child_id)

    _walk(node_id)
    return testcase_ids


async def create_report(
    db: AsyncSession,
    project_id: str,
    trigger_type: str,
    total_cases: int,
    task_id: str,
) -> ExecutionReport:
    report = ExecutionReport(
        id=str(uuid.uuid4()),
        task_id=task_id,
        project_id=project_id,
        trigger_type=trigger_type,
        total_cases=total_cases,
    )
    db.add(report)
    await db.flush()
    return report
