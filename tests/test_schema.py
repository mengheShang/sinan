"""Schema tests for the M1 tables.

These run the real migrations against a throwaway database created on the
server from TEST_DATABASE_URL, so they never touch existing data.
"""

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine

from app.db.models import (
    Account,
    AuditEvent,
    Credential,
    ExtractionRun,
    Item,
    ItemVersion,
    Message,
    SyncState,
)

pytestmark = pytest.mark.db

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
HASH = "0" * 64


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


def insert(conn: Connection, model: Any, **values: Any) -> uuid.UUID:
    """Insert one row and return its primary key."""
    stmt = sa.insert(model).values(**values)
    stmt = stmt.returning(*model.__table__.primary_key.columns)
    key: uuid.UUID = conn.execute(stmt).scalar_one()
    return key


def fails(conn: Connection, stmt: sa.Executable) -> None:
    """Assert the statement is rejected by a constraint or trigger."""
    savepoint = conn.begin_nested()
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(stmt)
    savepoint.rollback()


def new_account(conn: Connection, email: str = "student@example.edu") -> uuid.UUID:
    return insert(conn, Account, provider="gmail", email=email, role="school")


def new_message(conn: Connection, account_id: uuid.UUID, msg_id: str = "m1") -> uuid.UUID:
    return insert(
        conn,
        Message,
        account_id=account_id,
        provider_msg_id=msg_id,
        received_at=NOW,
        body_text="Homework 3 is now due Friday at 11:59pm PT",
    )


def new_version(conn: Connection) -> uuid.UUID:
    acct = new_account(conn)
    msg = new_message(conn, acct)
    item = insert(conn, Item, kind="deadline", title="Homework 3")
    return insert(
        conn,
        ItemVersion,
        item_id=item,
        version=1,
        change_type="create",
        actor="extraction",
        after={"title": "Homework 3"},
        evidence="Homework 3 is now due",
        message_id=msg,
    )


def test_no_drift_between_models_and_migrations(engine: Engine, db_url: str) -> None:
    command.check(alembic_config(db_url))


def test_downgrade_then_upgrade(engine: Engine, db_url: str) -> None:
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "base")
    inspector = sa.inspect(engine)
    assert "accounts" not in inspector.get_table_names()
    command.upgrade(cfg, "head")
    inspector = sa.inspect(engine)
    assert "audit_events" in inspector.get_table_names()


def test_account_unique_per_provider_and_email(conn: Connection) -> None:
    new_account(conn)
    dup = sa.insert(Account).values(
        provider="gmail",
        email="student@example.edu",
        role="personal",
    )
    fails(conn, dup)


def test_account_role_is_checked(conn: Connection) -> None:
    bad = sa.insert(Account).values(provider="gmail", email="a@example.com", role="boss")
    fails(conn, bad)


def test_one_credential_per_account_and_purpose(conn: Connection) -> None:
    acct = new_account(conn)
    values = {
        "account_id": acct,
        "purpose": "read",
        "ciphertext": b"not-a-real-token",
        "scopes": "gmail.readonly",
        "authorized_at": NOW,
    }
    insert(conn, Credential, **values)
    fails(conn, sa.insert(Credential).values(**values))
    bad = dict(values, purpose="admin")
    fails(conn, sa.insert(Credential).values(**bad))


def test_one_sync_state_per_account(conn: Connection) -> None:
    acct = new_account(conn)
    insert(conn, SyncState, account_id=acct)
    fails(conn, sa.insert(SyncState).values(account_id=acct))


def test_message_unique_per_account(conn: Connection) -> None:
    acct = new_account(conn)
    new_message(conn, acct, "m1")
    dup = sa.insert(Message).values(
        account_id=acct,
        provider_msg_id="m1",
        received_at=NOW,
    )
    fails(conn, dup)
    other = new_account(conn, "someone@example.com")
    new_message(conn, other, "m1")


def test_one_extraction_run_per_message_and_prompt(conn: Connection) -> None:
    msg = new_message(conn, new_account(conn))
    values = {
        "message_id": msg,
        "prompt_version": "v1",
        "prompt_hash": HASH,
        "status": "ok",
    }
    insert(conn, ExtractionRun, **values)
    fails(conn, sa.insert(ExtractionRun).values(**values))
    insert(conn, ExtractionRun, **dict(values, prompt_version="v2"))


def test_extraction_run_hash_must_be_sha256_hex(conn: Connection) -> None:
    msg = new_message(conn, new_account(conn))
    bad = sa.insert(ExtractionRun).values(
        message_id=msg,
        prompt_version="v1",
        prompt_hash="not-a-hash",
        status="ok",
    )
    fails(conn, bad)


def test_item_due_at_needs_due_raw(conn: Connection) -> None:
    bad = sa.insert(Item).values(kind="deadline", title="HW", due_at=NOW)
    fails(conn, bad)
    insert(conn, Item, kind="deadline", title="HW", due_at=NOW, due_raw="Nov 5 noon")


def test_item_status_is_checked(conn: Connection) -> None:
    bad = sa.insert(Item).values(kind="deadline", title="HW", status="maybe")
    fails(conn, bad)


def test_extraction_version_needs_evidence(conn: Connection) -> None:
    item = insert(conn, Item, kind="todo", title="Sign up")
    bad = sa.insert(ItemVersion).values(
        item_id=item,
        version=1,
        change_type="create",
        actor="extraction",
        after={"title": "Sign up"},
    )
    fails(conn, bad)


def test_item_version_numbers_are_unique(conn: Connection) -> None:
    version_id = new_version(conn)
    row = conn.execute(
        sa.select(ItemVersion.item_id, ItemVersion.message_id).where(ItemVersion.id == version_id)
    ).one()
    dup = sa.insert(ItemVersion).values(
        item_id=row.item_id,
        version=1,
        change_type="update",
        actor="extraction",
        after={},
        evidence="x",
        message_id=row.message_id,
    )
    fails(conn, dup)


def test_item_versions_are_append_only(conn: Connection) -> None:
    version_id = new_version(conn)
    where = ItemVersion.id == version_id
    fails(conn, sa.update(ItemVersion).where(where).values(evidence="edited"))
    fails(conn, sa.delete(ItemVersion).where(where))
    fails(conn, sa.text("TRUNCATE item_versions CASCADE"))


def test_item_with_versions_cannot_be_deleted(conn: Connection) -> None:
    version_id = new_version(conn)
    item_id = conn.execute(
        sa.select(ItemVersion.item_id).where(ItemVersion.id == version_id)
    ).scalar_one()
    fails(conn, sa.delete(Item).where(Item.id == item_id))


def test_audit_events_are_append_only(conn: Connection) -> None:
    event = insert(conn, AuditEvent, event_type="sync.run", actor="worker")
    where = AuditEvent.id == event
    fails(conn, sa.update(AuditEvent).where(where).values(actor="user"))
    fails(conn, sa.delete(AuditEvent).where(where))
    fails(conn, sa.text("TRUNCATE audit_events"))


def test_deleting_account_cascades_to_its_rows(conn: Connection) -> None:
    acct = new_account(conn)
    new_message(conn, acct)
    insert(conn, SyncState, account_id=acct)
    conn.execute(sa.delete(Account).where(Account.id == acct))
    count = conn.execute(sa.select(sa.func.count()).select_from(Message)).scalar_one()
    assert count == 0
