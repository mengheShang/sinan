"""Schema tests for `proposals` (migration 0004).

These use the throwaway database fixtures from conftest.py. Every name and
value is fictional.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Connection, Engine

from app.db.models import Account, ExtractionRun, Item, Message, Proposal
from app.proposals import payload_hash
from tests.conftest import alembic_config

pytestmark = pytest.mark.db

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
PAYLOAD = {"due_at": "2026-11-17T07:59:00Z", "title": "Homework 3"}


def insert(conn: Connection, model: Any, **values: Any) -> uuid.UUID:
    stmt = sa.insert(model).values(**values).returning(model.id)
    key: uuid.UUID = conn.execute(stmt).scalar_one()
    return key


def fails(conn: Connection, stmt: sa.Executable) -> None:
    """Assert the statement is rejected by a constraint or trigger."""
    savepoint = conn.begin_nested()
    with pytest.raises((sa.exc.IntegrityError, sa.exc.InternalError)):
        conn.execute(stmt)
    savepoint.rollback()


def sources(conn: Connection) -> dict[str, uuid.UUID]:
    """An item, a message and an extraction run a proposal can point at."""
    email = f"student-{uuid.uuid4().hex[:8]}@example.edu"
    acct = insert(conn, Account, provider="gmail", email=email, role="school")
    msg = insert(
        conn,
        Message,
        account_id=acct,
        provider_msg_id="m1",
        received_at=NOW,
        body_text="Homework 3 is now due Monday Nov 16 at 11:59pm PT.",
    )
    run = insert(
        conn,
        ExtractionRun,
        message_id=msg,
        prompt_version="v1",
        prompt_hash="0" * 64,
        status="ok",
    )
    item = insert(conn, Item, kind="deadline", title="Homework 3")
    return {"item_id": item, "message_id": msg, "extraction_run_id": run}


def values(conn: Connection, **changes: Any) -> dict[str, Any]:
    row = {
        **sources(conn),
        "kind": "item_update",
        "base_version": 1,
        "payload": PAYLOAD,
        "payload_hash": payload_hash(PAYLOAD),
        "expires_at": NOW + timedelta(days=7),
    }
    row.update(changes)
    return row


def new_proposal(conn: Connection, **changes: Any) -> uuid.UUID:
    return insert(conn, Proposal, **values(conn, **changes))


def set_status(pid: uuid.UUID, status: str, decided: bool) -> sa.Update:
    decided_at = NOW if decided else None
    where = Proposal.id == pid
    return sa.update(Proposal).where(where).values(status=status, decided_at=decided_at)


def status_of(conn: Connection, pid: uuid.UUID) -> str:
    query = sa.select(Proposal.status).where(Proposal.id == pid)
    status: str = conn.execute(query).scalar_one()
    return status


def test_no_drift_between_models_and_migrations(engine: Engine, db_url: str) -> None:
    command.check(alembic_config(db_url))


def test_new_proposal_is_pending(conn: Connection) -> None:
    pid = new_proposal(conn)
    assert status_of(conn, pid) == "pending"


def test_kind_is_checked(conn: Connection) -> None:
    fails(conn, sa.insert(Proposal).values(**values(conn, kind="send_email")))


def test_status_is_checked(conn: Connection) -> None:
    fails(conn, sa.insert(Proposal).values(**values(conn, status="maybe")))


@pytest.mark.parametrize("bad", ["not-a-hash", "A" * 64, "0" * 63, "0" * 65])
def test_hash_must_be_sha256_hex(conn: Connection, bad: str) -> None:
    fails(conn, sa.insert(Proposal).values(**values(conn, payload_hash=bad)))


def test_base_version_is_required(conn: Connection) -> None:
    fails(conn, sa.insert(Proposal).values(**values(conn, base_version=None)))


@pytest.mark.parametrize("bad", [0, -1])
def test_base_version_starts_at_one(conn: Connection, bad: int) -> None:
    fails(conn, sa.insert(Proposal).values(**values(conn, base_version=bad)))
    new_proposal(conn, base_version=3)


def test_payload_must_be_an_object(conn: Connection) -> None:
    fails(conn, sa.insert(Proposal).values(**values(conn, payload=["due_at"])))


def test_expiry_time_is_required(conn: Connection) -> None:
    fails(conn, sa.insert(Proposal).values(**values(conn, expires_at=None)))


@pytest.mark.parametrize("column", ["item_id", "message_id", "extraction_run_id"])
def test_sources_are_required(conn: Connection, column: str) -> None:
    fails(conn, sa.insert(Proposal).values(**values(conn, **{column: None})))


@pytest.mark.parametrize("column", ["item_id", "message_id", "extraction_run_id"])
def test_sources_must_exist(conn: Connection, column: str) -> None:
    row = values(conn, **{column: uuid.uuid4()})
    fails(conn, sa.insert(Proposal).values(**row))


@pytest.mark.parametrize("status", ["approved", "rejected", "executed"])
def test_decided_status_needs_decision_time(conn: Connection, status: str) -> None:
    fails(conn, sa.insert(Proposal).values(**values(conn, status=status)))
    new_proposal(conn, status=status, decided_at=NOW)


@pytest.mark.parametrize("status", ["pending", "expired", "superseded"])
def test_undecided_status_has_no_decision_time(conn: Connection, status: str) -> None:
    fails(conn, sa.insert(Proposal).values(**values(conn, status=status, decided_at=NOW)))
    new_proposal(conn, status=status)


@pytest.mark.parametrize(
    ("status", "decided"),
    [("approved", True), ("rejected", True), ("expired", False), ("superseded", False)],
)
def test_pending_can_be_closed(conn: Connection, status: str, decided: bool) -> None:
    pid = new_proposal(conn)
    conn.execute(set_status(pid, status, decided))
    assert status_of(conn, pid) == status


def test_approved_can_be_executed(conn: Connection) -> None:
    pid = new_proposal(conn)
    conn.execute(set_status(pid, "approved", True))
    conn.execute(set_status(pid, "executed", True))
    assert status_of(conn, pid) == "executed"


@pytest.mark.parametrize(
    ("start", "end", "decided"),
    [
        ("pending", "executed", True),
        ("approved", "rejected", True),
        ("approved", "pending", False),
        ("rejected", "approved", True),
        ("expired", "pending", False),
        ("expired", "approved", True),
        ("superseded", "pending", False),
        ("executed", "approved", True),
    ],
)
def test_other_status_moves_are_refused(
    conn: Connection, start: str, end: str, decided: bool
) -> None:
    start_decided = start in ("approved", "rejected", "executed")
    decided_at = NOW if start_decided else None
    pid = new_proposal(conn, status=start, decided_at=decided_at)
    fails(conn, set_status(pid, end, decided))


def test_decision_time_is_fixed_once_set(conn: Connection) -> None:
    pid = new_proposal(conn, status="approved", decided_at=NOW)
    later = sa.update(Proposal).where(Proposal.id == pid).values(decided_at=NOW + timedelta(1))
    fails(conn, later)


@pytest.mark.parametrize(
    "change",
    [
        {"payload": {"due_at": "2026-12-01T07:59:00Z", "title": "Homework 3"}},
        {"payload_hash": "f" * 64},
        {"base_version": 2},
        {"kind": "item_update", "expires_at": NOW + timedelta(days=30)},
        {"created_at": NOW},
    ],
)
def test_content_cannot_change(conn: Connection, change: dict[str, Any]) -> None:
    pid = new_proposal(conn)
    fails(conn, sa.update(Proposal).where(Proposal.id == pid).values(**change))


def test_proposals_cannot_be_deleted(conn: Connection) -> None:
    pid = new_proposal(conn, status="expired")
    fails(conn, sa.delete(Proposal).where(Proposal.id == pid))
    fails(conn, sa.text("TRUNCATE proposals"))


def test_target_item_cannot_be_deleted(conn: Connection) -> None:
    pid = new_proposal(conn)
    item = conn.execute(sa.select(Proposal.item_id).where(Proposal.id == pid)).scalar_one()
    fails(conn, sa.delete(Item).where(Item.id == item))


def test_downgrade_then_upgrade(engine: Engine, db_url: str) -> None:
    """Runs last in this module: it drops and recreates the table."""
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "0003")
    inspector = sa.inspect(engine)
    assert "proposals" not in inspector.get_table_names()
    assert "items" in inspector.get_table_names()
    with engine.connect() as c:
        query = "SELECT count(*) FROM pg_proc WHERE proname = 'sinan_proposal_guard'"
        assert c.execute(sa.text(query)).scalar_one() == 0
    command.upgrade(cfg, "head")
    inspector = sa.inspect(engine)
    assert "proposals" in inspector.get_table_names()
    columns = {c["name"]: c for c in inspector.get_columns("proposals")}
    assert columns["base_version"]["nullable"] is False
