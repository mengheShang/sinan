"""Base class for all ORM models. Alembic reads metadata from here."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
