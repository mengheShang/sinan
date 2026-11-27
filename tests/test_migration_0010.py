"""Migration 0010: `due_suggestions`, upgrade and downgrade.

The downgrade runs on a sibling database of its own, so the module's
database stays at head. Every name and value is fictional.
"""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine

from tests.conftest import alembic_config

pytestmark = pytest.mark.db


def current(engine: Engine) -> str:
    with engine.connect() as c:
        return str(c.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one())


def triggers(engine: Engine) -> set[str]:
    query = sa.text(
        "SELECT tgname FROM pg_trigger "
        "WHERE tgrelid = 'due_suggestions'::regclass AND NOT tgisinternal"
    )
    with engine.connect() as c:
        return set(c.execute(query).scalars())


def test_0010_follows_0009(db_url: str) -> None:
    script = ScriptDirectory.from_config(alembic_config(db_url))
    assert script.get_revision("0010").down_revision == "0009"


def test_the_table_is_append_only(engine: Engine) -> None:
    assert triggers(engine) == {"due_suggestions_no_change", "due_suggestions_no_truncate"}


@pytest.fixture
def sibling(db_url: str) -> Iterator[str]:
    base = sa.make_url(db_url)
    name = f"{base.database}_s10"
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
        command.downgrade(cfg, "0009")
        assert current(engine) == "0009"
        with engine.connect() as c:
            assert "due_suggestions" not in sa.inspect(c).get_table_names()
        command.upgrade(cfg, "head")
        assert current(engine) == "0011"
        assert len(triggers(engine)) == 2
    finally:
        engine.dispose()
