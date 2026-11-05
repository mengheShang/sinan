"""所有 ORM 模型的基类。Alembic 从这里读取元数据。"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
