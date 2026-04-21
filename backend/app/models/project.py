import uuid
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    tree_nodes: Mapped[list["TreeNode"]] = relationship(
        "TreeNode",
        back_populates="project",
        cascade="all, delete-orphan",
    )
    execution_reports: Mapped[list["ExecutionReport"]] = relationship(
        "ExecutionReport",
        back_populates="project",
        cascade="all, delete-orphan",
    )
