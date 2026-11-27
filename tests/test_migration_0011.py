"""Migration 0011: credential health columns, upgrade and downgrade.

The downgrade runs on a sibling database of its own, so the module's
database stays at head. Every name and value is fictional.
"""

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine

from tests.conftest import alembic_config

pytestmark = pytest.mark.db


def columns(engine: Engine) -> dict[str, Any]:
    with engine.connect() as c:
        found = sa.inspect(c).get_columns("credentials")
    return {col["name"]: col for col in found}


def test_0011_follows_0010(db_url: str) -> None:
    script = ScriptDirectory.from_config(alembic_config(db_url))
    assert script.get_revision("0011").down_revision == "0010"


def test_the_columns(engine: Engine) -> None:
    found = columns(engine)
    assert found["needs_reauth"]["nullable"] is False
    assert "false" in str(found["needs_reauth"]["default"])
    assert found["last_error"]["nullable"] is True


@pytest.fixture
def sibling(db_url: str) -> Iterator[str]:
    base = sa.make_url(db_url)
    name = f"{base.database}_s11"
    admin = sa.create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    command.upgrade(alembic_config(url), "head")
    yield url
    with admin.connect() as c:
        c.execute(sa.text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


def test_downgrade_then_upgrade(sibling: str) -> None:
    cfg = alembic_config(sibling)
    engine = sa.create_engine(sibling)
    try:
        command.downgrade(cfg, "0010")
        assert "needs_reauth" not in columns(engine)
        command.upgrade(cfg, "head")
        assert {"needs_reauth", "last_error"} <= set(columns(engine))
    finally:
        engine.dispose()
