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
