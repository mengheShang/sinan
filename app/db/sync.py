"""Sync one mailbox into `messages`.

Order matters for "no gaps, no duplicates":

1. Read the new cursor before listing mail, so anything that arrives during
   the run is caught by the next incremental sync.
2. Store messages. Each insert ignores rows that already exist, and messages
   are committed in batches, so a crash keeps what was saved and a rerun
   adds nothing twice.
3. Advance the cursor in a final commit, only after every message is saved.
   A crash before that leaves the cursor unchanged.

The caller supplies the session and a MailReader; no tokens pass through here.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import AuditEvent, Message, SyncState
from app.providers.gmail_parse import wanted
from app.providers.mail import HistoryExpiredError, MailReader, MessageNotFoundError

FIRST_WINDOW = timedelta(days=30)
# ADR 0002: after a cursor expires, re-sync from the last success, at most 30 days back
MAX_FALLBACK = timedelta(days=30)
BATCH = 50

Mode = Literal["initial", "incremental", "fallback"]


@dataclass(frozen=True)
class SyncResult:
    mode: Mode
    listed: int
    stored: int
    skipped: int


@dataclass(frozen=True)
class Plan:
    mode: Mode
    ids: list[str]
    cursor: str


def load_state(session: Session, account_id: uuid.UUID) -> SyncState:
    state = session.get(SyncState, account_id)
    if state is None:
        state = SyncState(account_id=account_id, needs_reauth=False)
        session.add(state)
        session.flush()
    return state


def window_start(last_success: datetime | None, now: datetime) -> datetime:
    """Where a fallback re-sync begins: the last success, but never over 30 days ago."""
    floor = now - MAX_FALLBACK
    if last_success is None or last_success < floor:
        return floor
    return last_success


def plan(
    reader: MailReader, cursor: str | None, last_success: datetime | None, now: datetime
) -> Plan:
    if cursor is None:
        profile = reader.get_profile()
        ids = reader.list_message_ids(now - FIRST_WINDOW)
        return Plan("initial", ids, profile.history_id)
    try:
        changes = reader.list_history(cursor)
    except HistoryExpiredError:
        profile = reader.get_profile()
        ids = reader.list_message_ids(window_start(last_success, now))
        return Plan("fallback", ids, profile.history_id)
    return Plan("incremental", changes.added_ids, changes.history_id)


def known_ids(session: Session, account_id: uuid.UUID, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    query = select(Message.provider_msg_id).where(
        Message.account_id == account_id,
        Message.provider_msg_id.in_(ids),
    )
    return set(session.execute(query).scalars())


def store(session: Session, account_id: uuid.UUID, reader: MailReader, ids: list[str]) -> int:
    """Fetch and insert each new, wanted message. Returns how many were stored."""
    stored = 0
    pending = 0
    for msg_id in ids:
        try:
            msg = reader.get_message(msg_id)
        except MessageNotFoundError:
            # Deleted between listing and fetching
            continue
        if not wanted(msg.label_ids):
            continue
        row = {
            "account_id": account_id,
            "provider_msg_id": msg.provider_msg_id,
            "thread_id": msg.thread_id,
            "sent_at": msg.sent_at,
            "received_at": msg.received_at,
            "from_addr": msg.from_addr,
            "subject": msg.subject,
            "headers": msg.headers,
            "label_ids": msg.label_ids,
            "body_text": msg.body_text,
        }
        stmt = insert(Message).values(**row)
        stmt = stmt.on_conflict_do_nothing(index_elements=["account_id", "provider_msg_id"])
        # RETURNING gives a row only when the insert happened
        inserted = session.execute(stmt.returning(Message.id)).first()
        stored += 0 if inserted is None else 1
        pending += 1
        if pending >= BATCH:
            session.commit()
            pending = 0
    session.commit()
    return stored


def audit(session: Session, account_id: uuid.UUID, data: dict[str, object]) -> None:
    event = AuditEvent(
        event_type="sync",
        actor="worker",
        account_id=account_id,
        subject_type="account",
        subject_id=account_id,
        data=data,
    )
    session.add(event)


def sync_account(
    session: Session,
    reader: MailReader,
    account_id: uuid.UUID,
    now: datetime,
) -> SyncResult:
    """Run one sync. On failure, record the error class and re-raise."""
    state = load_state(session, account_id)
    cursor = state.history_id
    last_success = state.last_success_at
    session.commit()
    try:
        steps = plan(reader, cursor, last_success, now)
        already = known_ids(session, account_id, steps.ids)
        fresh = [i for i in steps.ids if i not in already]
        stored = store(session, account_id, reader, fresh)
    except Exception as exc:
        session.rollback()
        record_failure(session, account_id, type(exc).__name__, now)
        raise
    state = load_state(session, account_id)
    state.history_id = steps.cursor
    state.last_attempt_at = now
    state.last_success_at = now
    state.last_error = None
    result = SyncResult(steps.mode, len(steps.ids), stored, len(steps.ids) - stored)
    audit(session, account_id, {"mode": result.mode, "listed": result.listed, "stored": stored})
    session.commit()
    return result


def record_failure(session: Session, account_id: uuid.UUID, error: str, now: datetime) -> None:
    """Keep the cursor; note when and how it failed (class name only, no content)."""
    state = load_state(session, account_id)
    state.last_attempt_at = now
    state.last_error = error
    audit(session, account_id, {"mode": "failed", "error": error})
    session.commit()
