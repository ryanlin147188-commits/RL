from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.permissions import require_permission
from app.auth.permissions_catalog import P
from app.auth.scope import ensure_project_in_scope
from app.database import get_db
from app.models.testcase_content import TestcaseContent
from app.models.tree_node import LevelType, TreeNode
from app.models.user import User
from app.schemas.testcase_content import (
    ImportJsonRequest,
    TestcaseContentResponse,
    TestcaseContentUpdate,
)

router = APIRouter()


async def _load_testcase_node(node_id: str, user: User, db: AsyncSession) -> TreeNode:
    """Fetch the TreeNode, enforce org scope, and confirm it is a TESTCASE."""
    node = await db.get(TreeNode, node_id)
    await ensure_project_in_scope(
        db, node.project_id if node else None, user, not_found_detail="Node not found"
    )
    if node.level_type != LevelType.TESTCASE:
        raise HTTPException(status_code=400, detail="Node is not a TESTCASE type")
    return node


# 7. GET /api/v1/testcases/{node_id}
@router.get(
    "/testcases/{node_id}",
    response_model=TestcaseContentResponse,
    dependencies=[Depends(require_permission(P.TESTCASE_READ))],
)
async def get_testcase(
    node_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _load_testcase_node(node_id, user, db)

    result = await db.execute(
        select(TestcaseContent).where(TestcaseContent.node_id == node_id)
    )
    content = result.scalar_one_or_none()

    if content is None:
        # 第一次開啟時自動建立空白內容
        content = TestcaseContent(node_id=node_id)
        db.add(content)
        await db.flush()
        await db.refresh(content)

    return content


# 8. PUT /api/v1/testcases/{node_id}
@router.put(
    "/testcases/{node_id}",
    response_model=TestcaseContentResponse,
    dependencies=[Depends(require_permission(P.TESTCASE_WRITE))],
)
async def update_testcase(
    node_id: str,
    payload: TestcaseContentUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """整包覆寫(前端將表格轉成 JSON 送來)。建議搭配版號欄位 (Optimistic Lock) 防止協作衝突。"""
    await _load_testcase_node(node_id, user, db)

    result = await db.execute(
        select(TestcaseContent).where(TestcaseContent.node_id == node_id)
    )
    content = result.scalar_one_or_none()
    if content is None:
        content = TestcaseContent(node_id=node_id)
        db.add(content)

    if payload.ac_text is not None:
        content.ac_text = payload.ac_text
    if payload.setup_text is not None:
        content.setup_text = payload.setup_text
    if payload.steps_json is not None:
        content.steps_json = payload.steps_json
    if payload.ddt_json is not None:
        content.ddt_json = payload.ddt_json

    await db.flush()
    await db.refresh(content)
    return content


# 9. POST /api/v1/testcases/{node_id}/import-json
@router.post(
    "/testcases/{node_id}/import-json",
    response_model=TestcaseContentResponse,
    dependencies=[Depends(require_permission(P.TESTCASE_WRITE))],
)
async def import_ddt_json(
    node_id: str,
    payload: ImportJsonRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """透過 JSON 批次更新 DDT 資料集。只覆寫 ddt_json 欄位,不影響 AC 與 BDD 步驟。
    預期格式:{"headers": ["$Acct", "$Pwd"], "rows": [["admin", "1234"]]}
    """
    await _load_testcase_node(node_id, user, db)

    # 驗證 ddt_json 結構(必須含 headers 與 rows)
    if "headers" not in payload.ddt_json or "rows" not in payload.ddt_json:
        raise HTTPException(
            status_code=400,
            detail='ddt_json 必須包含 "headers"(欄位名稱陣列)與 "rows"(資料列陣列)',
        )

    result = await db.execute(
        select(TestcaseContent).where(TestcaseContent.node_id == node_id)
    )
    content = result.scalar_one_or_none()
    if content is None:
        content = TestcaseContent(node_id=node_id)
        db.add(content)

    content.ddt_json = payload.ddt_json
    await db.flush()
    await db.refresh(content)
    return content
