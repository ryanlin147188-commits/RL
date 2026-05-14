"""Entity Version Service — AB 表設計的核心 service。

提供 ``snapshot()`` / ``list_versions()`` / ``revert_to()`` 三個 generic helper,
讓 6 個業務 entity (testcase / defect / requirement / test_document / wbs_item /
todo) 共用一致的版本管理邏輯。

工作流:
    人類編輯  →  snapshot(source='human', status='pending_review') → review_records 同步建 pending
    AI 生成   →  snapshot(source='ai',    status='ai_draft')        → 不進審核(等人選了再送)
    Review 通過 → 主表 content_status = 'approved' + snapshot(source='system', status='approved')
    Revert      → 主表覆蓋為某舊 version 內容 + snapshot(source='revert', status=該舊版的 status)

設計重點:
  * **registry 模式**:_REGISTRY 把 entity_type 字串 → (model class, 序列化欄位 list,
    optional content_loader)。新增 entity 時只要加一筆。
  * **JSON 序列化**:datetime / Enum 一律先轉成 isoformat / value;這樣
    revert 時直接 ``setattr(obj, k, v)`` 即可,SQLAlchemy 會自己解 enum。
  * **version_no 計算**:撈 max(version_no) + 1;併發寫入時 unique 沒卡住,
    若兩筆寫入拿到同樣 version_no DB 不會出錯,只是排序顯示稍微亂。
    可接受的 trade-off,不需 distributed lock。
"""
from __future__ import annotations

import enum
import logging
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entity_version import (
    EntityVersion,
    CHANGE_SOURCE_AI,
    CHANGE_SOURCE_HUMAN,
    CHANGE_SOURCE_REVERT,
    CHANGE_SOURCE_SYSTEM,
    CONTENT_STATUS_AI_DRAFT,
    CONTENT_STATUS_APPROVED,
    CONTENT_STATUS_PENDING,
    CONTENT_STATUS_REJECTED,
)

logger = logging.getLogger(__name__)


# ─── Registry: entity_type → ORM model + 序列化欄位 ─────────────────
class _EntitySpec:
    """每種 entity 的元資料:用什麼 model、要 mirror 哪些欄位。"""

    def __init__(
        self,
        model: Any,
        fields: list[str],
        *,
        loader: Optional[Callable] = None,
    ):
        self.model = model
        # 業務欄位(會進 snapshot 也會被 revert 蓋回去);**不**包含 id / project_id
        # / organization_id / created_at / updated_at 這種 metadata。
        self.fields = fields
        # optional:特殊 loader。預設用 db.get(model, id)。給有 testcase_content
        # 之類複合主表用的(目前 testcase 我們暫時只 mirror tree_node 的內容,
        # testcase_content 的 step 等動作後續可擴充)。
        self.loader = loader


def _build_registry() -> dict[str, _EntitySpec]:
    # 延後 import 避免 circular(models 裡會 import service 之前的東西)
    from app.models.defect import Defect
    from app.models.todo_item import TodoItem
    from app.models.tree_node import TreeNode

    return {
        "testcase": _EntitySpec(
            TreeNode,
            fields=[
                "name", "level_type", "parent_id", "sort_order",
                "assigned_to", "assigned_to_type",
            ],
        ),
        "defect": _EntitySpec(
            Defect,
            fields=[
                "code", "title", "description", "steps_to_reproduce",
                "expected_result", "actual_result",
                "severity", "priority", "status",
                "reporter", "assignee",
                "linked_testcase_id", "linked_report_id",
                "assigned_to", "assigned_to_type",
            ],
        ),
        "todo": _EntitySpec(
            TodoItem,
            fields=[
                "title", "description", "due_date", "status", "priority",
                "assigned_to", "assigned_to_type", "item_type",
                "parent_id", "sprint_label",
                "related_entity_type", "related_entity_id",
            ],
        ),
    }


_REGISTRY: dict[str, _EntitySpec] | None = None


def _get_registry() -> dict[str, _EntitySpec]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


def is_known_entity_type(entity_type: str) -> bool:
    return entity_type in _get_registry()


# ─── 序列化 / 反序列化 ─────────────────────────────────────────────
def _serialize_value(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, enum.Enum):
        return v.value if hasattr(v, "value") else str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _serialize_entity(entity: Any, fields: list[str]) -> dict[str, Any]:
    return {f: _serialize_value(getattr(entity, f, None)) for f in fields}


# ─── 公開 API ─────────────────────────────────────────────────────
async def snapshot(
    db: AsyncSession,
    *,
    entity_type: str,
    entity: Any,
    source: str,
    status: str,
    by: Optional[str] = None,
    reason: Optional[str] = None,
    parent_version_id: Optional[str] = None,
) -> EntityVersion:
    """為 entity 建立一筆新版本快照,並回傳。

    呼叫端負責確保 entity 的業務欄位已經設定到目標值(因為我們從 entity
    讀屬性);content_status 由本函式統一寫回 entity。
    """
    spec = _get_registry().get(entity_type)
    if spec is None:
        raise ValueError(f"Unknown entity_type: {entity_type}")
    if not isinstance(entity, spec.model):
        raise TypeError(
            f"entity must be {spec.model.__name__}, got {type(entity).__name__}"
        )

    # 序列化業務欄位
    snapshot_data = _serialize_entity(entity, spec.fields)
    # 同步把主表的 content_status 設成這次 snapshot 的 status
    if hasattr(entity, "content_status"):
        entity.content_status = status

    # 算 version_no
    last_no = (
        await db.execute(
            select(func.max(EntityVersion.version_no))
            .where(EntityVersion.entity_type == entity_type)
            .where(EntityVersion.entity_id == entity.id)
        )
    ).scalar()
    version_no = (last_no or 0) + 1

    org_id = getattr(entity, "organization_id", None)

    ev = EntityVersion(
        entity_type=entity_type,
        entity_id=entity.id,
        version_no=version_no,
        content_snapshot=snapshot_data,
        content_status=status,
        change_source=source,
        changed_by=by,
        change_reason=reason,
        parent_version_id=parent_version_id,
        organization_id=org_id,
    )
    db.add(ev)
    await db.flush()
    logger.info(
        "entity_version snapshot: %s/%s v%d status=%s source=%s by=%s",
        entity_type, entity.id, version_no, status, source, by,
    )
    return ev


async def list_versions(
    db: AsyncSession,
    *,
    entity_type: str,
    entity_id: str,
    organization_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """列出某 entity 的所有版本(版本由新到舊)。"""
    if not is_known_entity_type(entity_type):
        raise ValueError(f"Unknown entity_type: {entity_type}")
    stmt = (
        select(EntityVersion)
        .where(EntityVersion.entity_type == entity_type)
        .where(EntityVersion.entity_id == entity_id)
        .order_by(EntityVersion.version_no.desc())
    )
    if organization_id:
        stmt = stmt.where(EntityVersion.organization_id == organization_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": r.id,
            "version_no": r.version_no,
            "content_status": r.content_status,
            "change_source": r.change_source,
            "changed_by": r.changed_by,
            "change_reason": r.change_reason,
            "parent_version_id": r.parent_version_id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "snapshot": r.content_snapshot,
        }
        for r in rows
    ]


async def revert_to(
    db: AsyncSession,
    *,
    entity_type: str,
    entity_id: str,
    target_version_id: str,
    by: str,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """把 entity 還原到 target_version_id 的內容,並建一筆 source='revert' 的新版本。

    返回新建的版本資訊(dict)。
    """
    spec = _get_registry().get(entity_type)
    if spec is None:
        raise ValueError(f"Unknown entity_type: {entity_type}")
    target = (
        await db.execute(
            select(EntityVersion)
            .where(EntityVersion.id == target_version_id)
            .where(EntityVersion.entity_type == entity_type)
            .where(EntityVersion.entity_id == entity_id)
        )
    ).scalar_one_or_none()
    if not target:
        raise ValueError("target version not found for this entity")

    entity = await db.get(spec.model, entity_id)
    if not entity:
        raise ValueError("entity not found")

    # 套用 snapshot:從 JSON 把欄位寫回去
    for k in spec.fields:
        if k in target.content_snapshot:
            setattr(entity, k, target.content_snapshot[k])

    # 還原也視為新版本(content_status 沿用 target 的;source=revert,parent 指向 target)
    new_ver = await snapshot(
        db,
        entity_type=entity_type,
        entity=entity,
        source=CHANGE_SOURCE_REVERT,
        status=target.content_status,
        by=by,
        reason=(reason or f"Reverted to v{target.version_no}"),
        parent_version_id=target.id,
    )
    return {
        "id": new_ver.id,
        "version_no": new_ver.version_no,
        "content_status": new_ver.content_status,
        "reverted_from_version_no": target.version_no,
    }


# 公開常數,讓 router 不必直接 import enum 字串
__all__ = [
    "snapshot",
    "list_versions",
    "revert_to",
    "is_known_entity_type",
    "CHANGE_SOURCE_AI",
    "CHANGE_SOURCE_HUMAN",
    "CHANGE_SOURCE_SYSTEM",
    "CHANGE_SOURCE_REVERT",
    "CONTENT_STATUS_AI_DRAFT",
    "CONTENT_STATUS_PENDING",
    "CONTENT_STATUS_APPROVED",
    "CONTENT_STATUS_REJECTED",
]
