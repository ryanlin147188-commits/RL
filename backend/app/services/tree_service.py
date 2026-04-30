from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tree_node import LEVEL_HIERARCHY, LevelType, TreeNode


def build_tree(nodes: list[Any], parent_id: Optional[str] = None) -> list[dict]:
    """
    將資料庫回傳的扁平節點清單，在 Python 端遞迴組裝成樹狀 JSON。
    一次查詢全部節點、Python 端組樹，避免 N+1 問題。
    """
    result: list[dict] = []
    for node in sorted(nodes, key=lambda n: n.sort_order):
        if node.parent_id == parent_id:
            result.append(
                {
                    "id": node.id,
                    "project_id": node.project_id,
                    "parent_id": node.parent_id,
                    "level_type": node.level_type,
                    "name": node.name,
                    "sort_order": node.sort_order,
                    # Phase 2 — generic assignment metadata. Only TESTCASE
                    # nodes can be assigned (the router rejects others), but
                    # the columns exist on every row so we serialise them
                    # uniformly for simpler front-end caching.
                    "assigned_to": getattr(node, "assigned_to", None),
                    "assigned_to_type": getattr(node, "assigned_to_type", None),
                    "assigned_by": getattr(node, "assigned_by", None),
                    "assigned_at": getattr(node, "assigned_at", None).isoformat()
                        if getattr(node, "assigned_at", None) else None,
                    "children": build_tree(nodes, node.id),
                }
            )
    return result


async def get_expected_level(
    db: AsyncSession,
    project_id: str,
    parent_id: Optional[str],
) -> LevelType:
    """
    根據父節點層級，計算新節點應有的 level_type。
    同時做「防呆驗證」：不允許跨層插入節點。
    """
    if parent_id is None:
        return LevelType.FEATURE

    result = await db.execute(
        select(TreeNode).where(
            TreeNode.id == parent_id,
            TreeNode.project_id == project_id,
        )
    )
    parent = result.scalar_one_or_none()
    if parent is None:
        raise HTTPException(status_code=404, detail="Parent node not found in this project")

    expected = LEVEL_HIERARCHY.get(parent.level_type)
    if expected is None:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot add children under a {parent.level_type.value} node (leaf)",
        )
    return expected


async def recursive_delete(db: AsyncSession, node_id: str) -> None:
    """
    刪除節點及其所有子孫節點。
    依靠資料庫 FK ON DELETE CASCADE 處理巢狀刪除,
    execution_steps_log.testcase_node_id 設為 ON DELETE SET NULL(保留歷史)。

    Side effect (RFC-Review-1): also strip any ReviewRecord whose entity_id
    is somewhere in this subtree. The bulk DELETE below bypasses ORM events
    so the install_review_autocreate cascade hook never fires; we have to
    clean those rows explicitly here.
    """
    # 1) Collect every TESTCASE-level descendant id (only those have reviews).
    #    Walk the FK chain in SQL so we don't have to round-trip per level.
    from app.models.review import ReviewableEntityType, ReviewRecord

    descendant_ids = await _collect_testcase_descendants(db, node_id)

    if descendant_ids:
        await db.execute(
            sql_delete(ReviewRecord).where(
                ReviewRecord.entity_type == ReviewableEntityType.TESTCASE,
                ReviewRecord.entity_id.in_(descendant_ids),
            ).execution_options(synchronize_session=False)
        )

    await db.execute(
        sql_delete(TreeNode)
        .where(TreeNode.id == node_id)
        .execution_options(synchronize_session=False)
    )


async def _collect_testcase_descendants(
    db: AsyncSession, root_id: str
) -> list[str]:
    """Return every TESTCASE-level node id under ``root_id`` (inclusive).

    Uses a single recursive CTE so depth is bounded by SQL, not Python."""
    from sqlalchemy import text

    stmt = text(
        """
        WITH RECURSIVE subtree(id, level_type) AS (
            SELECT id, level_type FROM tree_nodes WHERE id = :root_id
            UNION ALL
            SELECT c.id, c.level_type
              FROM tree_nodes c
              JOIN subtree s ON c.parent_id = s.id
        )
        SELECT id FROM subtree WHERE level_type = 'TESTCASE'
        """
    )
    result = await db.execute(stmt, {"root_id": root_id})
    return [row[0] for row in result.fetchall()]
