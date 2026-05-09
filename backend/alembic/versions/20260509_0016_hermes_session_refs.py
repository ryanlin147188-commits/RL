"""drop ai_conversations / ai_messages, create hermes_session_refs

Revision ID: 0016_hermes_session_refs
Revises: 0015_precondition_env_binding
Create Date: 2026-05-09

PR3 主切換:廢掉舊 AI 對話兩張表(ai_conversations / ai_messages),改用
hermes_session_refs 一張薄 metadata 指標表 — 訊息內容由 Hermes sidecar 的
SQLite 全權管理,backend 只記 session_id 對應到誰、標題、最後訊息預覽。

行為差異(已在 plan / routers/hermes.py 註解):
- 舊歷史對話 / 訊息全部捨棄(沒備份;Hermes 的 SQLite 是新 store,沒法 import)。
- Downgrade 重建 schema 但資料無法恢復 — staging 跑 round-trip 只驗 schema 對稱。

idempotent:每個 op 先 inspect。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0016_hermes_session_refs"
down_revision: Union[str, None] = "0015_precondition_env_binding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    # ── 1) 先刪 ai_messages(FK 子表先)───────────────────────────────
    if _table_exists("ai_messages"):
        op.drop_table("ai_messages")
    # ── 2) 再刪 ai_conversations(parent)──────────────────────────────
    if _table_exists("ai_conversations"):
        op.drop_table("ai_conversations")
    # ── 3) 建新的 metadata 指標表 ────────────────────────────────────
    if not _table_exists("hermes_session_refs"):
        op.create_table(
            "hermes_session_refs",
            # PK 直接是 Hermes 回的 session_id(UUID 36) — 不再多 surrogate id
            sa.Column("id", sa.String(64), primary_key=True),
            # workspace_id = ws_<user.id>;sidecar 強制檢查 path 安全
            sa.Column("workspace_id", sa.String(64), nullable=False),
            sa.Column(
                "organization_id",
                sa.String(36),
                sa.ForeignKey("organizations.id", ondelete="SET NULL"),
                nullable=True,
            ),
            # username,沿用 ai_conversations 慣例
            sa.Column("owner", sa.String(100), nullable=False),
            sa.Column(
                "title",
                sa.String(200),
                nullable=False,
                server_default="新對話",
            ),
            sa.Column("last_message_preview", sa.String(200), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_hermes_session_refs_owner",
            "hermes_session_refs",
            ["owner"],
        )
        op.create_index(
            "ix_hermes_session_refs_workspace",
            "hermes_session_refs",
            ["workspace_id"],
        )
        op.create_index(
            "ix_hermes_session_refs_org",
            "hermes_session_refs",
            ["organization_id"],
        )


def downgrade() -> None:
    # 1) 移除新表
    if _table_exists("hermes_session_refs"):
        op.drop_table("hermes_session_refs")
    # 2) 重建舊 schema(空表 — 資料無法恢復;只讓 schema migration 可逆)
    if not _table_exists("ai_conversations"):
        op.create_table(
            "ai_conversations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "organization_id",
                sa.String(36),
                sa.ForeignKey("organizations.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("owner", sa.String(100), nullable=False),
            sa.Column(
                "title",
                sa.String(200),
                nullable=False,
                server_default="新對話",
            ),
            sa.Column("provider_config_id", sa.String(36), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_ai_conversations_owner", "ai_conversations", ["owner"],
        )
        op.create_index(
            "ix_ai_conversations_org", "ai_conversations", ["organization_id"],
        )
    if not _table_exists("ai_messages"):
        op.create_table(
            "ai_messages",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "conversation_id",
                sa.String(36),
                sa.ForeignKey("ai_conversations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("role", sa.String(20), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("tokens_used", sa.Integer(), nullable=True),
            sa.Column("provider", sa.String(40), nullable=True),
            sa.Column("model", sa.String(120), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_ai_messages_conv", "ai_messages", ["conversation_id"],
        )
