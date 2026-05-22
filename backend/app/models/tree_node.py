import enum
import uuid
from typing import Optional

from sqlalchemy import Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.auth.tenant import Assignable, TenantScoped
from .base import Base


class LevelType(str, enum.Enum):
    FEATURE = "FEATURE"
    PLATFORM = "PLATFORM"
    PAGE = "PAGE"
    SCENARIO = "SCENARIO"
    TESTCASE = "TESTCASE"


class WorkStatus(str, enum.Enum):
    """v1.1.9 加:測試看板 Kanban 上 testcase 的工作流狀態。

    跟 ExecutionReport / Defect 的狀態不一樣,這條是「人在追測試進度」的
    狀態,可由 user 在看板上拖拽改變。FEATURE / PLATFORM / PAGE / SCENARIO
    層級節點也有這欄位但不會顯示在看板上(看板只放 TESTCASE 級節點)。
    """
    NEW         = "NEW"          # 待測試
    IN_PROGRESS = "IN_PROGRESS"  # 測試中
    PASSED      = "PASSED"       # 已通過
    FAILED      = "FAILED"       # 失敗
    RETEST      = "RETEST"       # 複測中


# 合法的父子層級映射：None = 根節點，值為 None 表示不可再有子節點
LEVEL_HIERARCHY: dict[Optional[LevelType], Optional[LevelType]] = {
    None: LevelType.FEATURE,
    LevelType.FEATURE: LevelType.PLATFORM,
    LevelType.PLATFORM: LevelType.PAGE,
    LevelType.PAGE: LevelType.SCENARIO,
    LevelType.SCENARIO: LevelType.TESTCASE,
    LevelType.TESTCASE: None,  # 葉節點
}


class TreeNode(Assignable, TenantScoped, Base):
    __tablename__ = "tree_nodes"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("tree_nodes.id", ondelete="CASCADE"),
        nullable=True,
    )
    level_type: Mapped[LevelType] = mapped_column(Enum(LevelType), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # 生命週期狀態(配合 entity_versions 的 AB 設計):ai_draft / pending_review / approved / rejected
    # 預設 approved 是為了讓既有資料(舊版本沒這欄)直接視為已上線版,不會被審核 gate 擋住。
    content_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="approved", server_default="approved", index=True,
    )
    # 測試看板上的工作流狀態(v1.1.9)— 預設 NEW(待測試)。
    # 用 String + check constraint 簡化(避免新 enum 又要 migration);允許值
    # 對應 WorkStatus enum 的 .value。
    work_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="NEW", server_default="NEW", index=True,
    )

    # ── Relationships ──────────────────────────────────────────────
    project: Mapped["Project"] = relationship(
        "Project", back_populates="tree_nodes", lazy="noload"
    )
    parent: Mapped[Optional["TreeNode"]] = relationship(
        "TreeNode",
        back_populates="children",
        remote_side="TreeNode.id",
        foreign_keys="[TreeNode.parent_id]",
        lazy="noload",
    )
    children: Mapped[list["TreeNode"]] = relationship(
        "TreeNode",
        back_populates="parent",
        cascade="all, delete-orphan",
        foreign_keys="[TreeNode.parent_id]",
        order_by="TreeNode.sort_order",
        lazy="noload",
    )
    testcase_content: Mapped[Optional["TestcaseContent"]] = relationship(
        "TestcaseContent",
        back_populates="node",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="noload",
    )
