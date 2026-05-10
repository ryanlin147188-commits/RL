"""consolidate_lightweight_migrations -- move startup SQL into Alembic

Revision ID: 0005_lightweight_sql
Revises: 0004_assignment_invite_email
Create Date: 2026-05-03

Older app versions ran a best-effort block of ``ALTER`` / ``CREATE INDEX``
statements on every startup after Alembic. That split schema ownership between
``database.py`` and Alembic and hid failures by swallowing exceptions.

This revision absorbs those idempotent statements so startup only needs
``alembic upgrade head``. Most statements are no-ops for fresh databases
because the baseline creates tables from current metadata; they still repair
older deployments that were bootstrapped before the corresponding models or
indexes existed.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0005_lightweight_sql"
down_revision: Union[str, None] = "0004_assignment_invite_email"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


MIGRATION_STMTS: tuple[str, ...] = (
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS description TEXT",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS owner VARCHAR(100)",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS status VARCHAR(40)",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS start_date VARCHAR(20)",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS target_date VARCHAR(20)",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS tags VARCHAR(300)",
    "ALTER TABLE defects ADD COLUMN IF NOT EXISTS attachments_json JSON",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS organization_id VARCHAR(36)",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS organization_id VARCHAR(36)",
    "ALTER TABLE roles ADD COLUMN IF NOT EXISTS organization_id VARCHAR(36)",
    "ALTER TABLE email_configs ADD COLUMN IF NOT EXISTS organization_id VARCHAR(36)",
    "ALTER TABLE ai_token_configs ADD COLUMN IF NOT EXISTS organization_id VARCHAR(36)",
    "ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS organization_id VARCHAR(36)",
    "ALTER TABLE roles DROP CONSTRAINT IF EXISTS roles_name_key",
    "CREATE INDEX IF NOT EXISTS ix_users_org ON users (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_projects_org ON projects (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_roles_org ON roles (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_email_configs_org ON email_configs (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_ai_token_configs_org ON ai_token_configs (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_todo_items_org ON todo_items (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_oidc_providers_org ON oidc_providers (organization_id)",
    "ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS item_type VARCHAR(20) NOT NULL DEFAULT 'TASK'",
    "ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS parent_id VARCHAR(36)",
    "ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS sprint_label VARCHAR(80)",
    "CREATE INDEX IF NOT EXISTS ix_todo_items_parent ON todo_items (parent_id)",
    "CREATE INDEX IF NOT EXISTS ix_todo_items_sprint ON todo_items (sprint_label)",
    "CREATE INDEX IF NOT EXISTS ix_mock_endpoints_org ON mock_endpoints (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_mock_endpoints_project ON mock_endpoints (project_id)",
    "CREATE INDEX IF NOT EXISTS ix_db_configs_org ON db_configs (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_db_configs_project ON db_configs (project_id)",
    "ALTER TABLE todo_items ALTER COLUMN item_type TYPE VARCHAR(20) USING item_type::text",
    "ALTER TABLE todo_items ALTER COLUMN item_type SET DEFAULT 'TASK'",
    "DROP TYPE IF EXISTS todoitemtype CASCADE",
    "UPDATE todo_items SET item_type='FEATURE' WHERE item_type IN ('EPIC','Epic','Feature')",
    "UPDATE todo_items SET item_type='TASK' WHERE item_type IN ('Task')",
    "UPDATE todo_items SET item_type='BUG' WHERE item_type IN ('Bug')",
    "UPDATE todo_items SET item_type='SPIKE' WHERE item_type IN ('Spike')",
    (
        "UPDATE todo_items SET item_type='FEATURE', parent_id=NULL "
        "WHERE item_type IN ('STORY','Story')"
    ),
    "CREATE INDEX IF NOT EXISTS ix_todo_links_todo ON todo_links (todo_id)",
    "CREATE INDEX IF NOT EXISTS ix_todo_links_target ON todo_links (target_type, target_id)",
    "CREATE INDEX IF NOT EXISTS ix_todo_links_org ON todo_links (organization_id)",
    # ai_conversations / ai_messages 在 0016 才會被 drop;新部署的 baseline 從未建立它們,
    # 因此這幾條索引只對「升級自舊版」的部署有意義,fresh DB 直接跳過。
    # 條件由 upgrade() 中以 information_schema 判斷後執行。
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url VARCHAR(500)",
    "ALTER TABLE ai_token_configs ADD COLUMN IF NOT EXISTS reasoning_effort VARCHAR(10)",
    "ALTER TABLE ai_token_configs ALTER COLUMN provider TYPE VARCHAR(40) USING provider::text",
    "DROP TYPE IF EXISTS aiprovider CASCADE",
    "ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS assignee_type VARCHAR(10) NOT NULL DEFAULT 'user'",
    "ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS assigned_by VARCHAR(80)",
    "ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMP",
    "CREATE INDEX IF NOT EXISTS ix_groups_org ON groups (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_groups_parent ON groups (parent_id)",
    "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS email_domains VARCHAR(500)",
    "CREATE INDEX IF NOT EXISTS ix_org_invites_org ON org_invites (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_test_versions_org ON test_versions (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_test_versions_project ON test_versions (project_id)",
    "ALTER TABLE execution_reports ADD COLUMN IF NOT EXISTS test_version_id VARCHAR(36)",
    "ALTER TABLE defects ADD COLUMN IF NOT EXISTS test_version_id VARCHAR(36)",
    "ALTER TABLE test_rounds ADD COLUMN IF NOT EXISTS test_version_id VARCHAR(36)",
    "CREATE INDEX IF NOT EXISTS ix_execution_reports_tv ON execution_reports (test_version_id)",
    "CREATE INDEX IF NOT EXISTS ix_defects_tv ON defects (test_version_id)",
    "CREATE INDEX IF NOT EXISTS ix_test_rounds_tv ON test_rounds (test_version_id)",
)


_LEGACY_AI_INDEX_STMTS: tuple[tuple[str, str], ...] = (
    ("ai_conversations", "CREATE INDEX IF NOT EXISTS ix_ai_conversations_owner ON ai_conversations (owner)"),
    ("ai_conversations", "CREATE INDEX IF NOT EXISTS ix_ai_conversations_org ON ai_conversations (organization_id)"),
    ("ai_messages", "CREATE INDEX IF NOT EXISTS ix_ai_messages_conv ON ai_messages (conversation_id)"),
)


def _table_exists(bind, name: str) -> bool:
    row = bind.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = :n"
        ),
        {"n": name},
    ).first()
    return row is not None


def upgrade() -> None:
    bind = op.get_bind()
    for stmt in MIGRATION_STMTS:
        bind.execute(text(stmt))
    for table_name, stmt in _LEGACY_AI_INDEX_STMTS:
        if _table_exists(bind, table_name):
            bind.execute(text(stmt))


def downgrade() -> None:
    # Intentionally no-op. This revision consolidates historical, best-effort
    # startup repair SQL. Reversing it safely would risk dropping data-bearing
    # columns from existing deployments; full downgrade-to-base still drops all
    # tables through the baseline revision.
    pass
