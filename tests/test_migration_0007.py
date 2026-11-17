"""Migration 0007: the `notice` kind on items, upgrade and downgrade.

The module's throwaway database keeps committed notices, so the clean
downgrade runs on a sibling database of its own. Every name and value is
fictional.
"""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine

from app.db.models import Item
from tests.conftest import alembic_config

pytestmark = pytest.mark.db


def current(engine: Engine) -> str:
    with engine.connect() as c:
        return str(c.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one())


def add_item(engine: Engine, kind: str) -> None:
    with engine.begin() as c:
        c.execute(sa.insert(Item).values(kind=kind, title="Fictional item"))


def head(db_url: str) -> str:
    script = ScriptDirectory.from_config(alembic_config(db_url))
    found = script.get_current_head()
    assert found is not None
    return found


def test_0007_follows_0006(db_url: str) -> None:
    script = ScriptDirectory.from_config(alembic_config(db_url))
    assert script.get_revision("0007").down_revision == "0006"


def test_notice_kind_allowed(engine: Engine) -> None:
    add_item(engine, "notice")
    add_item(engine, "todo")
    with pytest.raises(sa.exc.IntegrityError):
        add_item(engine, "announcement")


def test_downgrade_refuses_while_notices_exist(engine: Engine, db_url: str) -> None:
    add_item(engine, "notice")
    cfg = alembic_config(db_url)
    with pytest.raises(RuntimeError, match="kind 'notice'"):
        command.downgrade(cfg, "0006")
    # Nothing changed: still at head, and notices are still allowed
    assert current(engine) == head(db_url)
    add_item(engine, "notice")


@pytest.fixture
def clean(db_url: str) -> Iterator[str]:
    """A sibling database with no notices, so a downgrade is allowed."""
    base = sa.make_url(db_url)
    name = f"{base.database}_clean7"
    admin = sa.create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    command.upgrade(alembic_config(url), "head")
    yield url
    with admin.connect() as c:
        c.execute(sa.text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


def test_downgrade_then_upgrade_without_notices(clean: str) -> None:
    cfg = alembic_config(clean)
    engine = sa.create_engine(clean)
    try:
        add_item(engine, "todo")
        command.downgrade(cfg, "0006")
        assert current(engine) == "0006"
        with pytest.raises(sa.exc.IntegrityError):
            add_item(engine, "notice")
        command.upgrade(cfg, "head")
        assert current(engine) == head(clean)
        add_item(engine, "notice")
    finally:
        engine.dispose()
