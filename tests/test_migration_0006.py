"""Migration 0006: the `system` actor on item versions, upgrade and downgrade.

The module's throwaway database keeps committed `system` rows, which can
never be removed, so the clean downgrade runs on a sibling database of its
own. Every name and value is fictional.
"""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine

from app.db.models import Item, ItemVersion
from tests.conftest import alembic_config

pytestmark = pytest.mark.db


def current(engine: Engine) -> str:
    with engine.connect() as c:
        return str(c.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one())


def add_version(engine: Engine, actor: str) -> None:
    """A committed item with one version made by `actor`."""
    with engine.begin() as c:
        item_id = c.execute(
            sa.insert(Item).values(kind="todo", title="Fictional task").returning(Item.id)
        ).scalar_one()
        values = {
            "item_id": item_id,
            "version": 1,
            "change_type": "status",
            "actor": actor,
            "after": {"status": "needs_review"},
        }
        c.execute(sa.insert(ItemVersion).values(**values))


def test_0006_is_head_after_0005(db_url: str) -> None:
    script = ScriptDirectory.from_config(alembic_config(db_url))
    assert script.get_current_head() == "0006"
    assert script.get_revision("0006").down_revision == "0005"


def test_system_actor_allowed(engine: Engine) -> None:
    add_version(engine, "system")
    with pytest.raises(sa.exc.IntegrityError):
        add_version(engine, "robot")


def test_downgrade_refuses_while_system_rows_exist(engine: Engine, db_url: str) -> None:
    add_version(engine, "system")
    cfg = alembic_config(db_url)
    with pytest.raises(RuntimeError, match="actor 'system'.*append-only"):
        command.downgrade(cfg, "0005")
    # Nothing changed: still at 0006, and system rows are still allowed
    assert current(engine) == "0006"
    add_version(engine, "system")


@pytest.fixture
def clean(db_url: str) -> Iterator[str]:
    """A sibling database with no system rows, so a downgrade is allowed."""
    base = sa.make_url(db_url)
    name = f"{base.database}_clean"
    admin = sa.create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    command.upgrade(alembic_config(url), "head")
    yield url
    with admin.connect() as c:
        c.execute(sa.text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


def test_downgrade_then_upgrade_without_system_rows(clean: str) -> None:
    cfg = alembic_config(clean)
    engine = sa.create_engine(clean)
    try:
        add_version(engine, "user")
        command.downgrade(cfg, "0005")
        assert current(engine) == "0005"
        with pytest.raises(sa.exc.IntegrityError):
            add_version(engine, "system")
        command.upgrade(cfg, "head")
        assert current(engine) == "0006"
        add_version(engine, "system")
    finally:
        engine.dispose()
