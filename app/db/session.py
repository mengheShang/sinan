"""数据库引擎和连通性检查。"""

from functools import lru_cache

from sqlalchemy import Engine, create_engine, text

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(settings.database_url, pool_pre_ping=True)


def check_db(engine: Engine) -> bool:
    """执行 SELECT 1，连接正常时返回 True。"""
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))
        value = result.scalar_one()
    return bool(value == 1)
