"""The dev run's in-memory Authentication-Results fetch (ADR 0010), on synthetic data."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.db.models import Account, Message
from app.providers.demo_data import gmail_auth
from app.providers.fakes import FakeMailReader
from app.providers.mail import MailMessage, MailReader
from evals import dataset
from evals.real import RealSet, with_auth
from evals.run import SETS

WHEN = datetime(2026, 11, 10, 17, 0, tzinfo=UTC)


@pytest.fixture
def session(conn: Connection) -> Session:
    return Session(bind=conn, join_transaction_mode="create_savepoint")


def stored(session: Session, owner: uuid.UUID, msg_id: str) -> uuid.UUID:
    row = Message(
        account_id=owner,
        provider_msg_id=msg_id,
        received_at=WHEN,
        from_addr="rivera@example.edu",
        headers={},
        label_ids=["INBOX"],
        body_text="Fictional body",
    )
    session.add(row)
    session.flush()
    return row.id


def test_auth_is_added_in_memory_only(session: Session) -> None:
    owner = Account(provider="gmail", email="student@example.edu", role="school")
    session.add(owner)
    session.flush()
    samples = dataset.load(SETS["synthetic"].samples)[:2]
    first = stored(session, owner.id, "m1")
    second = stored(session, owner.id, "m2")
    auth = gmail_auth("rivera@example.edu")
    mail = MailMessage(
        "m1", None, WHEN, WHEN, "rivera@example.edu", "s", "b", {"Authentication-Results": auth}
    )
    reader = FakeMailReader("student@example.edu", [mail])

    def reader_for(_: Session, account: uuid.UUID, __: str) -> MailReader | None:
        return reader if account == owner.id else None

    found = RealSet(1, samples, {}, {samples[0].id: first, samples[1].id: second})
    fetched = with_auth(session, found, reader_for)
    assert (fetched.fetched, fetched.missing) == (1, 1)
    assert fetched.samples[0].input.headers["Authentication-Results"] == auth
    assert fetched.samples[1] == samples[1]
    # Nothing is written back
    assert session.get_one(Message, first).headers == {}
