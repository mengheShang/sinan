"""Mail sync tests against a throwaway database and the fake mailbox."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.db.models import Account, AuditEvent, Credential, Message, SyncState
from app.db.sync import MAX_FALLBACK, sync_account, window_start
from app.providers.fakes import FakeMailReader
from app.providers.mail import MailMessage, MailReader
from apps.worker.sync_job import sync_all

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
EMAIL = "student@example.edu"


def msg(n: int, days_ago: int, labels: list[str] | None = None) -> MailMessage:
    when = NOW - timedelta(days=days_ago)
    return MailMessage(
        provider_msg_id=f"m{n:03d}",
        thread_id=None,
        received_at=when,
        sent_at=when,
        from_addr="rivera@example.edu",
        subject=f"Fictional notice {n}",
        body_text="Synthetic body.",
        label_ids=labels if labels is not None else ["INBOX"],
    )


class CrashingReader(FakeMailReader):
    """Fails on one message, like a network error halfway through a run."""

    def __init__(self, email: str, messages: list[MailMessage], crash_on: str) -> None:
        super().__init__(email, messages)
        self.crash_on = crash_on

    def get_message(self, provider_msg_id: str) -> MailMessage:
        if provider_msg_id == self.crash_on:
            raise RuntimeError("connection reset")
        return super().get_message(provider_msg_id)


class GoneReader(FakeMailReader):
    """Lists a message that was deleted before it could be fetched."""

    def list_message_ids(self, since: datetime) -> list[str]:
        return [*super().list_message_ids(since), "deleted-id"]


def make_session(conn: Connection) -> Session:
    return Session(bind=conn, join_transaction_mode="create_savepoint")


@pytest.fixture
def session(conn: Connection) -> Session:
    return make_session(conn)


def add_account(session: Session, email: str) -> uuid.UUID:
    """An account with a placeholder read credential; sync never decrypts it here."""
    stmt = sa.insert(Account).values(provider="gmail", email=email, role="school")
    account_id: uuid.UUID = session.execute(stmt.returning(Account.id)).scalar_one()
    cred = Credential(
        account_id=account_id,
        purpose="read",
        ciphertext=b"placeholder",
        scopes="https://www.googleapis.com/auth/gmail.readonly",
        authorized_at=NOW,
    )
    session.add(cred)
    session.commit()
    return account_id


@pytest.fixture
def account(session: Session) -> uuid.UUID:
    return add_account(session, EMAIL)


def count(session: Session) -> int:
    query = sa.select(sa.func.count()).select_from(Message)
    total: int = session.execute(query).scalar_one()
    return total


def state(session: Session, account_id: uuid.UUID) -> SyncState:
    session.expire_all()
    row = session.get(SyncState, account_id)
    assert row is not None
    return row


def run(session: Session, reader: MailReader, account: uuid.UUID, now: datetime = NOW) -> str:
    return sync_account(session, reader, account, now).mode


pytestmark = pytest.mark.db


def test_first_sync_takes_the_last_30_days(session: Session, account: uuid.UUID) -> None:
    reader = FakeMailReader(EMAIL, [msg(1, 40), msg(2, 29), msg(3, 1)])
    result = sync_account(session, reader, account, NOW)
    assert result.mode == "initial"
    assert count(session) == 2
    row = state(session, account)
    assert row.history_id == "3"
    assert row.last_success_at == NOW
    assert row.last_error is None


def test_sync_twice_adds_nothing(session: Session, account: uuid.UUID) -> None:
    reader = FakeMailReader(EMAIL, [msg(1, 3), msg(2, 2)])
    assert run(session, reader, account) == "initial"
    second = sync_account(session, reader, account, NOW)
    assert second.mode == "incremental"
    assert second.stored == 0
    assert count(session) == 2


def test_incremental_picks_up_new_mail(session: Session, account: uuid.UUID) -> None:
    reader = FakeMailReader(EMAIL, [msg(1, 3)])
    run(session, reader, account)
    reader.add(msg(2, 0))
    result = sync_account(session, reader, account, NOW)
    assert result.mode == "incremental"
    assert result.stored == 1
    assert state(session, account).history_id == "2"


def test_expired_cursor_falls_back_to_last_success(
    session: Session,
    account: uuid.UUID,
) -> None:
    reader = FakeMailReader(EMAIL, [msg(1, 5)])
    run(session, reader, account, NOW - timedelta(days=4))
    reader.add(msg(2, 3))
    reader.add(msg(3, 1))
    reader.expire_history_before("3")
    result = sync_account(session, reader, account, NOW)
    assert result.mode == "fallback"
    assert count(session) == 3
    assert state(session, account).history_id == "3"


def test_fallback_window_is_capped_at_30_days() -> None:
    long_ago = NOW - timedelta(days=90)
    assert window_start(long_ago, NOW) == NOW - MAX_FALLBACK
    assert window_start(None, NOW) == NOW - MAX_FALLBACK
    recent = NOW - timedelta(days=2)
    assert window_start(recent, NOW) == recent


def test_crash_before_commit_keeps_the_cursor(session: Session, account: uuid.UUID) -> None:
    messages = [msg(1, 3), msg(2, 2), msg(3, 1)]
    crashing = CrashingReader(EMAIL, messages, crash_on="m002")
    with pytest.raises(RuntimeError):
        sync_account(session, crashing, account, NOW)
    row = state(session, account)
    assert row.history_id is None
    assert row.last_success_at is None
    assert row.last_error == "RuntimeError"
    healthy = FakeMailReader(EMAIL, messages)
    assert run(session, healthy, account) == "initial"
    assert count(session) == 3


def test_unwanted_labels_are_skipped(session: Session, account: uuid.UUID) -> None:
    messages = [
        msg(1, 1, ["INBOX"]),
        msg(2, 1, ["SPAM"]),
        msg(3, 1, ["SENT"]),
        msg(4, 1, ["DRAFT"]),
        msg(5, 1, ["Label_1"]),
    ]
    run(session, FakeMailReader(EMAIL, messages), account)
    stored = set(session.execute(sa.select(Message.provider_msg_id)).scalars())
    assert stored == {"m001", "m005"}


def test_deleted_message_is_skipped(session: Session, account: uuid.UUID) -> None:
    run(session, GoneReader(EMAIL, [msg(1, 1)]), account)
    assert count(session) == 1


def test_every_run_is_audited(session: Session, account: uuid.UUID) -> None:
    reader = FakeMailReader(EMAIL, [msg(1, 1)])
    run(session, reader, account)
    crashing = CrashingReader(EMAIL, [msg(1, 1), msg(2, 0)], crash_on="m002")
    crashing.expire_history_before("99")
    with pytest.raises(RuntimeError):
        sync_account(session, crashing, account, NOW)
    events = session.execute(sa.select(AuditEvent).order_by(AuditEvent.occurred_at)).scalars()
    data = [e.data for e in events]
    assert data[0] == {"mode": "initial", "listed": 1, "stored": 1}
    assert data[1] == {"mode": "failed", "error": "RuntimeError"}
    for item in data:
        assert "Synthetic" not in str(item)


def test_sync_all_skips_accounts_that_need_reauth(conn: Connection, account: uuid.UUID) -> None:
    with make_session(conn) as setup:
        setup.add(SyncState(account_id=account, needs_reauth=True))
        setup.commit()
    seen: list[uuid.UUID] = []

    def reader_for(session: Session, account_id: uuid.UUID, email: str) -> MailReader | None:
        seen.append(account_id)
        return FakeMailReader(EMAIL, [msg(1, 1)])

    assert sync_all(lambda: make_session(conn), reader_for, lambda: NOW) == 0
    assert seen == []


def test_sync_all_keeps_going_after_a_failure(conn: Connection, account: uuid.UUID) -> None:
    with make_session(conn) as setup:
        add_account(setup, "other@example.com")

    def reader_for(session: Session, account_id: uuid.UUID, email: str) -> MailReader | None:
        if email == EMAIL:
            return CrashingReader(EMAIL, [msg(1, 1)], crash_on="m001")
        return FakeMailReader(email, [msg(2, 1)])

    assert sync_all(lambda: make_session(conn), reader_for, lambda: NOW) == 1
    with make_session(conn) as check:
        assert count(check) == 1
