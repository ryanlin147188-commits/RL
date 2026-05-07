"""Alembic env.

DSN is sourced from a fresh ``Settings()`` instance(而不是 module-level
``settings``)。這讓測試夾具(testcontainers)在 alembic 啟動前才設好的
``DB_HOST`` / ``DB_PORT`` 等環境變數能被讀到 — 否則 pytest 啟動時
``--cov=app`` 提早 import ``app.config``,``settings`` 已經用預設值固定下
來,後續 testcontainers 改寫的環境變數就被忽略,migration 會連到不存在的
``localhost:5432``。

We import app.models so that every model is registered against Base.metadata
before autogenerate runs.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import Settings
from app.models import Base
import app.models  # noqa: F401  — ensure all models are loaded into Base.metadata

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: alembic.ini's [logger_root] section would
    # otherwise reset every app-level logger to "disabled", silently swallowing
    # log.info / log.warning calls from request handlers after the very first
    # init_db() runs at startup.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Inject runtime DSN(sync driver — psycopg v3),用「現在」的 env 重建 Settings,
# 而不是 import 階段那份舊的 module-level singleton。
config.set_main_option("sqlalchemy.url", Settings().SYNC_DATABASE_URL)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emits SQL without a live DB)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live DB."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
