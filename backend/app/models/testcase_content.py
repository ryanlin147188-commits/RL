from typing import Any

from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class TestcaseContent(Base):
    """
    1 對 1 對應 tree_nodes（level_type = TESTCASE）。
    steps_json 格式：[{"id":"...","keyword":"Given","action":"...", ...}, ...]
    ddt_json   格式：{"headers":["$Acct","$Pwd"], "rows":[["admin","1234"]]}
    """

    __tablename__ = "testcase_contents"

    node_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tree_nodes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ac_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    setup_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    steps_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    ddt_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)

    node: Mapped["TreeNode"] = relationship(
        "TreeNode", back_populates="testcase_content", lazy="noload"
    )
