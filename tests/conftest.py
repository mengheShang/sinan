"""Shared fixtures.

Database tests run the real migrations against a throwaway database created
on the server from TEST_DATABASE_URL, so they never touch existing data.
"""

import os
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine


def alembic_config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="module")
def db_url() -> Iterator[str]:
    base = os.environ.get("TEST_DATABASE_URL")
    if not base:
        pytest.skip("TEST_DATABASE_URL is not set")
    name = f"sinan_test_{uuid.uuid4().hex[:8]}"
    admin = sa.create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = sa.make_url(base).set(database=name)
    url_str = url.render_as_string(hide_password=False)
    yield url_str
    with admin.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture(scope="module")
def engine(db_url: str) -> Iterator[Engine]:
    command.upgrade(alembic_config(db_url), "head")
    eng = sa.create_engine(db_url)
    yield eng
    eng.dispose()


@pytest.fixture
def conn(engine: Engine) -> Iterator[Connection]:
    """A connection whose work is rolled back after each test."""
    with engine.connect() as c:
        trans = c.begin()
        yield c
        trans.rollback()
