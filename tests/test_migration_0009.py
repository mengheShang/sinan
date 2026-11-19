"""Migration 0009: grants, actions and the wider proposals, upgrade and downgrade.

The module's throwaway database keeps a committed grant, so the clean
downgrade runs on a sibling database of its own. Every name and value is
fictional.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine

from app.db.models import Account, Grant
from tests.conftest import alembic_config

pytestmark = pytest.mark.db


def current(engine: Engine) -> str:
    with engine.connect() as c:
        return str(c.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one())


def head(db_url: str) -> str:
    script = ScriptDirectory.from_config(alembic_config(db_url))
    found = script.get_current_head()
    assert found is not None
    return found


def add_grant(engine: Engine) -> None:
    email = f"student-{uuid.uuid4().hex[:8]}@example.edu"
    with engine.begin() as c:
        stmt = sa.insert(Account).values(provider="gmail", email=email, role="personal")
        acct = c.execute(stmt.returning(Account.id)).scalar_one()
        expires = datetime.now(UTC) + timedelta(days=30)
        values = {
            "account_id": acct,
            "senders": ["news@example.com"],
            "kinds": ["mark_read"],
            "expires_at": expires,
        }
        c.execute(sa.insert(Grant).values(**values))


def test_0009_follows_0008(db_url: str) -> None:
    script = ScriptDirectory.from_config(alembic_config(db_url))
    assert script.get_revision("0009").down_revision == "0008"


def test_downgrade_refuses_while_grants_exist(engine: Engine, db_url: str) -> None:
    add_grant(engine)
    with pytest.raises(RuntimeError, match="1 grants exist"):
        command.downgrade(alembic_config(db_url), "0008")
    assert current(engine) == head(db_url)


@pytest.fixture
def clean(db_url: str) -> Iterator[str]:
    """A sibling database with no M3 rows, so a downgrade is allowed."""
    base = sa.make_url(db_url)
    name = f"{base.database}_clean9"
    admin = sa.create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    command.upgrade(alembic_config(url), "head")
    yield url
    with admin.connect() as c:
        c.execute(sa.text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


def test_downgrade_then_upgrade_without_m3_rows(clean: str) -> None:
    cfg = alembic_config(clean)
    engine = sa.create_engine(clean)
    try:
        command.downgrade(cfg, "0008")
        assert current(engine) == "0008"
        with engine.connect() as c:
            tables = sa.inspect(c).get_table_names()
        assert "grants" not in tables
        assert "actions" not in tables
        command.upgrade(cfg, "head")
        assert current(engine) == head(clean)
        add_grant(engine)
    finally:
        engine.dispose()
