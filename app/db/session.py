"""Database engine and connectivity check."""

from functools import lru_cache

from sqlalchemy import Engine, create_engine, text

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(settings.database_url, pool_pre_ping=True)


def check_db(engine: Engine) -> bool:
    """Run SELECT 1 and return True if the connection works."""
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))
        value = result.scalar_one()
    return bool(value == 1)
