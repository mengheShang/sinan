"""Labeling emails from the Dashboard into the label table (Issue #40).

Builds on app/db/labels.py: saving freezes the snapshot and adds the row,
correcting retires the old row and adds its replacement, and skipping
records an audit event. Each runs in the caller's transaction, so the API
commits once per request.

Local database writes only: nothing reaches Gmail and the model plays no
part. Audit events hold IDs and statuses only, never email content, and
nothing here logs.

Only emails of the current draw are in the queue (Issue #41): the dev set
first, then the test set. Within a set they are labeled oldest first, so the
item an extension changes is usually labeled before the extension. The
current draw of a set is its newest set version, including the supplement
of mail likely to hold a deadline (Issue #78), whose emails sit among the
original dev draw in date order.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import ColumnElement, case, exists, func, select, tuple_
from sqlalchemy.orm import Session
from sqlalchemy.sql import Subquery

from app.db import labels as store
from app.db import sampling
from app.db.label_format import Labels, Snapshot, missing_due_raw
from app.db.models import Account, AuditEvent, EvalLabel, Message

SKIP_EVENT = "eval_label.skip"


class UnknownEmail(Exception):
    pass


class UnknownLabel(Exception):
    pass


class Conflict(Exception):
    """The email or label is not in the state the request expects."""


@dataclass(frozen=True)
class Problem:
    """One reason a label cannot be stored: a field path and a message, no values."""

    field: str
    message: str


class Invalid(Exception):
    def __init__(self, problems: list[Problem]) -> None:
        super().__init__("; ".join(p.field for p in problems))
        self.problems = problems


def problems_of(err: ValidationError, prefix: str) -> list[Problem]:
    """Field paths and messages only: the input values are left out."""
    found = []
    details = err.errors(include_url=False, include_input=False, include_context=False)
    for detail in details:
        parts = [prefix, *(str(p) for p in detail["loc"])]
        path = ".".join(p for p in parts if p)
        found.append(Problem(path or "label", detail["msg"]))
    return found


# ---- The queue ----
#
# The queue holds only emails of the newest version of each set (Issue #41),
# with its supplement (Issue #78): the dev set first, then the test set, each
# oldest first.

SET_RANK = {"dev": 0, "test": 1}

Place = tuple[int, datetime, uuid.UUID]


def order_key() -> ColumnElement[datetime]:
    # The time the extractor uses: sent, or received when sent is missing
    return func.coalesce(Message.sent_at, Message.received_at)


def rank(sampled: Subquery) -> ColumnElement[int]:
    return case(SET_RANK, value=sampled.c.eval_set, else_=len(SET_RANK))


def current_label(message_id: Any) -> ColumnElement[bool]:
    live = EvalLabel.retired_at.is_(None)
    return exists().where(EvalLabel.source_message_id == message_id, live)


def skipped(message_id: Any) -> ColumnElement[bool]:
    is_skip = AuditEvent.event_type == SKIP_EVENT
    return exists().where(is_skip, AuditEvent.subject_id == message_id)


def is_open() -> ColumnElement[bool]:
    """Neither labeled nor skipped."""
    return ~current_label(Message.id) & ~skipped(Message.id)


@dataclass(frozen=True)
class Counts:
    # Labeled emails in each set's current draw, and the size of that draw
    dev: int
    test: int
    dev_total: int
    test_total: int
    skipped: int
    remaining: int


def counts(session: Session) -> Counts:
    sampled = sampling.current(session)
    labeled = func.count().filter(current_label(sampled.c.message_id))
    by_set = select(sampled.c.eval_set, func.count(), labeled).group_by(sampled.c.eval_set)
    per_set = {name: (total, done) for name, total, done in session.execute(by_set)}
    in_draw = Message.id.in_(select(sampled.c.message_id))
    only_skipped = skipped(Message.id) & ~current_label(Message.id)
    skip_query = select(func.count()).select_from(Message).where(in_draw, only_skipped)
    open_query = select(func.count()).select_from(Message).where(in_draw, is_open())
    dev = per_set.get("dev", (0, 0))
    test = per_set.get("test", (0, 0))
    return Counts(
        dev=dev[1],
        test=test[1],
        dev_total=dev[0],
        test_total=test[0],
        skipped=session.execute(skip_query).scalar_one(),
        remaining=session.execute(open_query).scalar_one(),
    )


def first_id(
    session: Session,
    open_only: bool = False,
    after: Place | None = None,
    before: Place | None = None,
) -> uuid.UUID | None:
    """The first sampled email in queue order after `after`.

    With `before`, the last one before it instead.
    """
    sampled = sampling.current(session)
    keys: list[ColumnElement[Any]] = [rank(sampled), order_key(), Message.id.expression]
    place = tuple_(*keys)
    query = select(Message.id).join(sampled, sampled.c.message_id == Message.id)
    if open_only:
        query = query.where(is_open())
    if after is not None:
        query = query.where(place > tuple_(*after))
    if before is not None:
        query = query.where(place < tuple_(*before))
        keys = [k.desc() for k in keys]
    query = query.order_by(*keys).limit(1)
    found: uuid.UUID | None = session.execute(query).scalar_one_or_none()
    return found


def next_open(session: Session, message_id: uuid.UUID | None = None) -> uuid.UUID | None:
    """The next email to label after this one, wrapping to the first."""
    if message_id is not None:
        place = position(session, message_id)
        found = first_id(session, open_only=True, after=place)
        if found is not None:
            return found
    return first_id(session, open_only=True)


@dataclass(frozen=True)
class Drawn:
    eval_set: str
    set_version: int


def drawn(session: Session, message_id: uuid.UUID) -> Drawn | None:
    """The set and set version the current draw gives this email, if any."""
    sampled = sampling.current(session)
    where = sampled.c.message_id == message_id
    query = select(sampled.c.eval_set, sampled.c.set_version).where(where)
    row = session.execute(query).one_or_none()
    if row is None:
        return None
    return Drawn(row[0], row[1])


def position(session: Session, message_id: uuid.UUID) -> Place:
    """Where a sampled email sits in the queue. Unsampled emails are unknown."""
    found = drawn(session, message_id)
    if found is None:
        raise UnknownEmail(str(message_id))
    query = select(order_key()).where(Message.id == message_id)
    key: datetime = session.execute(query).scalar_one()
    return SET_RANK[found.eval_set], key, message_id


# ---- One email ----


@dataclass(frozen=True)
class EmailView:
    """What the labeling page shows. Holds email content: never log it."""

    id: uuid.UUID
    from_addr: str | None
    subject: str | None
    body_text: str | None
    sent_at: datetime
    role: str
    # True when the text above is the frozen snapshot of a stored label
    frozen: bool
    label_id: uuid.UUID | None
    eval_set: str | None
    labels: dict[str, Any] | None
    tags: list[str]
    skipped: bool
    # Why the email cannot be frozen as it is, as field paths and messages
    problems: list[Problem]
    prev_id: uuid.UUID | None
    next_id: uuid.UUID | None
    next_open_id: uuid.UUID | None
    # Refs of items in earlier labeled emails, for `changes`
    refs: list[dict[str, str]] = field(default_factory=list)


def current_row(session: Session, message_id: uuid.UUID) -> EvalLabel | None:
    query = select(EvalLabel).where(
        EvalLabel.source_message_id == message_id,
        EvalLabel.retired_at.is_(None),
    )
    row: EvalLabel | None = session.execute(query).scalar_one_or_none()
    return row


def freeze_problems(session: Session, message_id: uuid.UUID) -> list[Problem]:
    try:
        store.freeze(session, message_id)
    except ValidationError as err:
        return problems_of(err, "email")
    return []


def earlier_refs(
    session: Session, sent_at: datetime, skip: uuid.UUID | None
) -> list[dict[str, str]]:
    """Items of current labels whose email came before `sent_at`."""
    found = []
    for row in current_rows(session, skip):
        if row.sent_at >= sent_at:
            continue
        for item in row.labels.items:
            found.append({"ref": item.ref, "title": item.title, "eval_set": row.eval_set})
    return found


def read_email(session: Session, message_id: uuid.UUID) -> EmailView:
    query = (
        select(Message, Account.role)
        .join(Account, Account.id == Message.account_id)
        .where(Message.id == message_id)
    )
    found = session.execute(query).one_or_none()
    if found is None:
        raise UnknownEmail(str(message_id))
    msg, role = found
    place = position(session, message_id)
    pick = drawn(session, message_id)
    row = current_row(session, message_id)
    sent_at = msg.sent_at or msg.received_at
    view: dict[str, Any] = {
        "from_addr": msg.from_addr,
        "subject": msg.subject,
        "body_text": msg.body_text,
        "sent_at": sent_at,
        "role": role,
    }
    if row is not None:
        # Show what was labeled, even if the message changed since
        snap = Snapshot.model_validate(row.snapshot)
        view = snap.model_dump(exclude={"headers"})
    problems = [] if row is not None else freeze_problems(session, message_id)
    return EmailView(
        id=message_id,
        frozen=row is not None,
        label_id=row.id if row else None,
        eval_set=row.eval_set if row else pick.eval_set if pick else None,
        labels=row.labels if row else None,
        tags=list(row.tags) if row else [],
        skipped=is_skipped(session, message_id),
        problems=problems,
        prev_id=first_id(session, before=place),
        next_id=first_id(session, after=place),
        next_open_id=next_open(session, message_id),
        refs=earlier_refs(session, view["sent_at"], row.id if row else None),
        **view,
    )


def is_skipped(session: Session, message_id: uuid.UUID) -> bool:
    query = select(skipped(message_id))
    return bool(session.execute(query).scalar_one())


# ---- Checks against the rest of the set ----


@dataclass(frozen=True)
class Stored:
    id: uuid.UUID
    eval_set: str
    sent_at: datetime
    labels: Labels


def current_rows(session: Session, skip: uuid.UUID | None) -> list[Stored]:
    """Every current label, without the row `skip` (the one being corrected)."""
    query = select(EvalLabel.id, EvalLabel.eval_set, EvalLabel.snapshot, EvalLabel.labels)
    query = query.where(EvalLabel.retired_at.is_(None))
    rows = []
    for row in session.execute(query):
        if row.id == skip:
            continue
        snap = Snapshot.model_validate(row.snapshot)
        labels = Labels.model_validate(row.labels)
        rows.append(Stored(row.id, row.eval_set, snap.sent_at, labels))
    return rows


def link_problems(
    session: Session,
    snapshot: Snapshot,
    labels: Labels,
    skip: uuid.UUID | None = None,
) -> list[Problem]:
    """The rules evals/dataset.py checks when a set loads, applied before storing.

    Refs are unique among current labels, and `changes` names an item from
    an earlier email. Refs are checked across both sets, so moving an email
    between sets later cannot clash.
    """
    taken: dict[str, datetime] = {}
    for row in current_rows(session, skip):
        for item in row.labels.items:
            taken[item.ref] = row.sent_at
    found = []
    seen: set[str] = set()
    for n, item in enumerate(labels.items):
        path = f"labels.items.{n}"
        if item.ref in taken or item.ref in seen:
            found.append(Problem(f"{path}.ref", "already used by another item"))
        seen.add(item.ref)
        if item.changes is None:
            continue
        target = taken.get(item.changes)
        if target is None or target >= snapshot.sent_at:
            found.append(Problem(f"{path}.changes", "must name an item from an earlier email"))
    for ref in missing_due_raw(snapshot, labels):
        n = next(i for i, item in enumerate(labels.items) if item.ref == ref)
        found.append(Problem(f"labels.items.{n}.due_raw", "must appear in the subject or body"))
    return found


# ---- Writes ----


def audit(event: str, subject_type: str, subject_id: uuid.UUID, data: dict[str, Any]) -> AuditEvent:
    return AuditEvent(
        event_type=event,
        actor="user",
        subject_type=subject_type,
        subject_id=subject_id,
        data=data,
    )


def save(
    session: Session,
    message_id: uuid.UUID,
    labels: Labels,
    tags: list[str],
) -> uuid.UUID:
    """Freeze the email and store its first label, in the set its draw gave it.

    Does not commit. Emails outside the current draw are unknown here.

    The message row is locked first, so two saves of the same email cannot
    both pass the check; the unique index on current rows backs this up.
    """
    lock = select(Message.id).where(Message.id == message_id).with_for_update()
    if session.execute(lock).scalar_one_or_none() is None:
        raise UnknownEmail(str(message_id))
    pick = drawn(session, message_id)
    if pick is None:
        raise UnknownEmail(str(message_id))
    if current_row(session, message_id) is not None:
        raise Conflict("already labeled")
    try:
        snapshot = store.freeze(session, message_id)
    except ValidationError as err:
        raise Invalid(problems_of(err, "email")) from None
    problems = link_problems(session, snapshot, labels)
    if problems:
        raise Invalid(problems)
    label_id = store.add(
        session, snapshot, labels, pick.eval_set, pick.set_version, message_id, tags
    )
    data = {
        "message_id": str(message_id),
        "eval_set": pick.eval_set,
        "set_version": pick.set_version,
    }
    session.add(audit("eval_label.add", "eval_label", label_id, data))
    session.flush()
    return label_id


def correct(
    session: Session,
    label_id: uuid.UUID,
    labels: Labels,
    tags: list[str],
) -> uuid.UUID:
    """Retire a current label and store its correction. Does not commit."""
    lock = select(EvalLabel).where(EvalLabel.id == label_id).with_for_update()
    old = session.execute(lock).scalar_one_or_none()
    if old is None:
        raise UnknownLabel(str(label_id))
    if old.retired_at is not None:
        raise Conflict("label is not current")
    snapshot = Snapshot.model_validate(old.snapshot)
    problems = link_problems(session, snapshot, labels, skip=label_id)
    if problems:
        raise Invalid(problems)
    new_id = store.correct(session, label_id, labels, tags)
    data = {"corrects": str(label_id), "eval_set": old.eval_set}
    session.add(audit("eval_label.correct", "eval_label", new_id, data))
    session.flush()
    return new_id


def skip(session: Session, message_id: uuid.UUID) -> None:
    """Leave an email out of the queue. Does not commit.

    Skipping is recorded as an audit event; there is no skip table. An
    email skipped twice is recorded once. A skipped email can still be
    labeled.
    """
    lock = select(Message.id).where(Message.id == message_id).with_for_update()
    if session.execute(lock).scalar_one_or_none() is None:
        raise UnknownEmail(str(message_id))
    if drawn(session, message_id) is None:
        raise UnknownEmail(str(message_id))
    if current_row(session, message_id) is not None:
        raise Conflict("already labeled")
    if is_skipped(session, message_id):
        return
    session.add(audit(SKIP_EVENT, "message", message_id, {}))
    session.flush()
