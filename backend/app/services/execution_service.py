import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_report import ExecutionReport
from app.models.tree_node import LevelType, TreeNode


async def collect_testcase_ids(db: AsyncSession, node_id: str) -> list[str]:
    """
    收集指定節點（含其所有子孫節點）下的全部 TESTCASE 節點 ID。
    使用單次查詢 + Python 端遞迴，避免多次 DB 往返。

    展開順序依 TreeNode.sort_order(同 parent 內),以對齊使用者在樹上看到
    的順序;沒有顯式排序時 SQL 返回的 row order 不可靠。
    """
    # 一次撈出同 project 下全部節點
    node_result = await db.execute(select(TreeNode).where(TreeNode.id == node_id))
    root = node_result.scalar_one_or_none()
    if root is None:
        return []

    all_nodes_result = await db.execute(
        select(TreeNode)
        .where(TreeNode.project_id == root.project_id)
        .order_by(TreeNode.sort_order, TreeNode.id)
    )
    all_nodes = all_nodes_result.scalars().all()

    # 建立 id -> node 映射
    node_map: dict[str, TreeNode] = {n.id: n for n in all_nodes}

    # 建立 parent_id -> children 映射(因為前面已 order_by sort_order,
    # 同 parent 的 children 自然會按樹順 append 進 list)
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
    execution_mode: str = "docker",
    source_node_id: str | None = None,
    source_node_ids: list[str] | None = None,
    ddt_expand: bool = False,
    enable_recording: bool = True,
) -> ExecutionReport:
    report = ExecutionReport(
        id=str(uuid.uuid4()),
        task_id=task_id,
        project_id=project_id,
        trigger_type=trigger_type,
        execution_mode=(execution_mode or "docker").lower(),
        total_cases=total_cases,
        source_node_id=source_node_id,
        source_node_ids=list(source_node_ids) if source_node_ids else None,
        ddt_expand=bool(ddt_expand),
        enable_recording=bool(enable_recording),
    )
    db.add(report)
    await db.flush()
    return report
