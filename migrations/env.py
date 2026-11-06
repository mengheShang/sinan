"""Alembic environment. The database URL comes from app.config, not alembic.ini."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

import app.db.models  # noqa: F401  import every model so autogenerate can see them
from app.config import get_settings
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """Use a URL passed in by the caller (tests), otherwise app settings."""
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    settings = get_settings()
    return settings.database_url


def run_migrations_offline() -> None:
    """Emit SQL without connecting to the database."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect to the database and run migrations."""
    engine = create_engine(get_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
