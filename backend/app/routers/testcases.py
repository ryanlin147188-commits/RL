from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.permissions import require_casbin
from app.auth.permissions_catalog import P
from app.auth.scope import ensure_project_in_scope
from app.database import get_db
from app.models.review import ReviewableEntityType
from app.services import review_service
from app.models.testcase_content import TestcaseContent
from app.models.testcase_env_binding import TestcaseEnvBinding
from app.models.testcase_precondition_link import TestcasePreconditionLink
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
    dependencies=[Depends(require_casbin(P.TESTCASE_READ))],
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
    dependencies=[Depends(require_casbin(P.TESTCASE_WRITE))],
)
async def update_testcase(
    node_id: str,
    payload: TestcaseContentUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """整包覆寫(前端將表格轉成 JSON 送來)。建議搭配版號欄位 (Optimistic Lock) 防止協作衝突。"""
    await _load_testcase_node(node_id, user, db)
    # RFC-Review-1: approved test cases are locked from edits until reverted.
    await review_service.ensure_not_approved(
        db,
        entity_type=ReviewableEntityType.TESTCASE,
        entity_id=node_id,
        organization_id=None if user.is_superuser else user.organization_id,
    )

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
    dependencies=[Depends(require_casbin(P.TESTCASE_WRITE))],
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


# ── Precondition links (前置案例)─────────────────────────────────────


class PreconditionLinkInput(BaseModel):
    precondition_testcase_id: str = Field(..., min_length=1, max_length=36)
    sort_order: int = 0
    enabled: bool = True
    on_failure: str = Field(default="stop", pattern="^(stop)$")


class PreconditionLinkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    testcase_id: str
    precondition_testcase_id: str
    sort_order: int
    enabled: bool
    on_failure: str


class PreconditionListReplaceRequest(BaseModel):
    """整批覆寫:傳入的列表會完全取代既有 precondition_links。"""

    items: list[PreconditionLinkInput] = Field(default_factory=list)


@router.get(
    "/testcases/{node_id}/preconditions",
    response_model=list[PreconditionLinkResponse],
    dependencies=[Depends(require_casbin(P.TESTCASE_READ))],
    tags=["B · 測試案例編輯"],
)
async def list_preconditions(
    node_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _load_testcase_node(node_id, user, db)
    rows = (
        await db.execute(
            select(TestcasePreconditionLink)
            .where(TestcasePreconditionLink.testcase_id == node_id)
            .order_by(TestcasePreconditionLink.sort_order, TestcasePreconditionLink.id)
        )
    ).scalars().all()
    return list(rows)


def _detect_precondition_cycle(
    edges: dict[str, set[str]], root: str
) -> Optional[str]:
    """DFS:從 root 出發若會走回 root 即回傳卡到的節點 id;否則 None。"""
    on_path: set[str] = set()

    def _dfs(node: str) -> Optional[str]:
        if node in on_path:
            return node
        on_path.add(node)
        for nxt in edges.get(node, set()):
            hit = _dfs(nxt)
            if hit is not None:
                return hit
        on_path.remove(node)
        return None

    return _dfs(root)


@router.put(
    "/testcases/{node_id}/preconditions",
    response_model=list[PreconditionLinkResponse],
    dependencies=[Depends(require_casbin(P.TESTCASE_WRITE))],
    tags=["B · 測試案例編輯"],
)
async def replace_preconditions(
    node_id: str,
    payload: PreconditionListReplaceRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """整批覆寫某 testcase 的前置案例清單。
    內建檢查:
      * 不可指向自己
      * 每個 precondition 必須是同一個 project 的 TESTCASE
      * 不可形成循環(本案例的前置(含巢狀)不能繞回自己)
      * 同一對 (testcase, precondition) 不可重複
    """
    node = await _load_testcase_node(node_id, user, db)

    seen_ids: set[str] = set()
    new_pre_ids: list[str] = []
    for item in payload.items:
        pid = item.precondition_testcase_id
        if pid == node_id:
            raise HTTPException(400, "前置案例不可指向自己")
        if pid in seen_ids:
            raise HTTPException(400, f"重複的前置案例: {pid}")
        seen_ids.add(pid)
        new_pre_ids.append(pid)

    if new_pre_ids:
        rows = (
            await db.execute(
                select(TreeNode.id, TreeNode.project_id, TreeNode.level_type).where(
                    TreeNode.id.in_(new_pre_ids)
                )
            )
        ).all()
        type_map = {tid: ltype for tid, _, ltype in rows}
        proj_map = {tid: pid for tid, pid, _ in rows}
        for pid in new_pre_ids:
            if pid not in type_map:
                raise HTTPException(404, f"前置案例不存在: {pid}")
            if type_map[pid] != LevelType.TESTCASE:
                raise HTTPException(400, f"前置案例必須是 TESTCASE 葉節點: {pid}")
            if proj_map[pid] != node.project_id:
                raise HTTPException(400, f"前置案例必須與本案例屬於同一個 project: {pid}")

        # 循環偵測:用「擬寫入」後的圖,從每個 new_pre_id 出發看是否會走回 node_id
        existing_edges: dict[str, set[str]] = {}
        all_links = (
            await db.execute(
                select(
                    TestcasePreconditionLink.testcase_id,
                    TestcasePreconditionLink.precondition_testcase_id,
                ).where(TestcasePreconditionLink.enabled.is_(True))
            )
        ).all()
        for tc, pre in all_links:
            existing_edges.setdefault(tc, set()).add(pre)
        # 把本次更新放進去:先清掉本案例的舊 outgoing,再加上新的
        existing_edges[node_id] = set(new_pre_ids)
        # 從本案例 DFS 看會不會繞回自己
        on_path: set[str] = set()

        def _has_cycle(start: str) -> Optional[str]:
            on_path.add(start)
            for nxt in existing_edges.get(start, set()):
                if nxt == node_id:
                    return nxt
                if nxt in on_path:
                    return nxt
                hit = _has_cycle(nxt)
                if hit is not None:
                    return hit
            on_path.remove(start)
            return None

        for pid in new_pre_ids:
            on_path.clear()
            on_path.add(node_id)
            hit = _has_cycle(pid)
            if hit is not None:
                raise HTTPException(
                    400,
                    f"加上 {pid} 會形成前置案例循環(碰到 {hit})",
                )

    # 整批覆寫
    await db.execute(
        delete(TestcasePreconditionLink).where(
            TestcasePreconditionLink.testcase_id == node_id
        )
    )
    new_rows: list[TestcasePreconditionLink] = []
    for item in payload.items:
        link = TestcasePreconditionLink(
            testcase_id=node_id,
            precondition_testcase_id=item.precondition_testcase_id,
            sort_order=item.sort_order,
            enabled=item.enabled,
            on_failure=item.on_failure,
            created_by=user.username,
        )
        db.add(link)
        new_rows.append(link)
    await db.flush()
    return new_rows


# ── Env bindings (環境變數綁定)──────────────────────────────────────


class EnvBindingInput(BaseModel):
    env_var_name: str = Field(..., min_length=1, max_length=120)


class EnvBindingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    testcase_id: str
    env_var_name: str


class EnvBindingListReplaceRequest(BaseModel):
    """整批覆寫:傳入的名稱列表會完全取代既有 env_bindings。"""

    items: list[str] = Field(default_factory=list)


@router.get(
    "/testcases/{node_id}/env-bindings",
    response_model=list[EnvBindingResponse],
    dependencies=[Depends(require_casbin(P.TESTCASE_READ))],
    tags=["B · 測試案例編輯"],
)
async def list_env_bindings(
    node_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _load_testcase_node(node_id, user, db)
    rows = (
        await db.execute(
            select(TestcaseEnvBinding)
            .where(TestcaseEnvBinding.testcase_id == node_id)
            .order_by(TestcaseEnvBinding.env_var_name)
        )
    ).scalars().all()
    return list(rows)


@router.put(
    "/testcases/{node_id}/env-bindings",
    response_model=list[EnvBindingResponse],
    dependencies=[Depends(require_casbin(P.TESTCASE_WRITE))],
    tags=["B · 測試案例編輯"],
)
async def replace_env_bindings(
    node_id: str,
    payload: EnvBindingListReplaceRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """整批覆寫某 testcase 綁定的 env var name 清單。

    不檢查 env var 在 project_env_vars 內是否實際存在 — 沒設定的會在執行時
    被當成空字串(同 docker / local runner 規則)。前端可以另外提示「未設定」。
    """
    await _load_testcase_node(node_id, user, db)

    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in payload.items:
        name = (raw or "").strip()
        if not name:
            continue
        if name in seen:
            continue
        seen.add(name)
        cleaned.append(name)

    await db.execute(
        delete(TestcaseEnvBinding).where(TestcaseEnvBinding.testcase_id == node_id)
    )
    new_rows: list[TestcaseEnvBinding] = []
    for name in cleaned:
        row = TestcaseEnvBinding(
            testcase_id=node_id,
            env_var_name=name,
            created_by=user.username,
        )
        db.add(row)
        new_rows.append(row)
    await db.flush()
    return new_rows
