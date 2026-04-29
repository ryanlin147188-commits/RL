"""Alembic env.

DSN is sourced from app.config.settings.SYNC_DATABASE_URL so that all DB
configuration (host / port / user / password / dbname) lives in one place.

We import app.models so that every model is registered against Base.metadata
before autogenerate runs.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.models import Base
import app.models  # noqa: F401  — ensure all models are loaded into Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject runtime DSN (sync driver — psycopg v3)
config.set_main_option("sqlalchemy.url", settings.SYNC_DATABASE_URL)

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
