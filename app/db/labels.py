"""Write and read evaluation labels with frozen snapshots (ADR 0009, decision 3).

Writing: `freeze` copies a message's extractor inputs once, when it is
labeled. `add` stores a new labeled row, `correct` retires a row and stores
its replacement with the same snapshot, and `retire` takes a row out of its
set. None of them edits a stored row; the database enforces that too.

`set_version` says which sampling draw a row belongs to. It is bumped when a
set is redrawn, and a correction keeps the old row's version.

Reading: `load_set` reads only `eval_labels`, never `messages`, and returns
samples in the format of evals/README.md, checked by evals/dataset.py.

Snapshots and labels of real sets hold real mail. Nothing here logs them.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, insert, select, update
from sqlalchemy.orm import Session

from app.db.models import Account, EvalLabel, Message
from evals.dataset import HEADER_NAMES, Labels, Sample, Snapshot


class LabelError(ValueError):
    """The request cannot be met. The message holds ids only, never content."""


def utc_text(when: datetime) -> str:
    text = when.astimezone(UTC).isoformat()
    return text.replace("+00:00", "Z")


def freeze(session: Session, message_id: uuid.UUID) -> Snapshot:
    """Copy exactly what the extractor and the prefilter see of one message."""
    query = (
        select(Message, Account.role)
        .join(Account, Account.id == Message.account_id)
        .where(Message.id == message_id)
    )
    row = session.execute(query).one_or_none()
    if row is None:
        raise LabelError(f"message {message_id} not found")
    msg, role = row
    # The extractor uses the sent time, or the received time when it is missing
    sent_at = msg.sent_at or msg.received_at
    headers = {name: value for name, value in msg.headers.items() if name in HEADER_NAMES}
    data = {
        "from_addr": msg.from_addr,
        "subject": msg.subject or "",
        "body_text": msg.body_text,
        "sent_at": utc_text(sent_at),
        "role": role,
        "headers": headers,
    }
    return Snapshot.model_validate(data)


def add(
    session: Session,
    snapshot: Snapshot,
    labels: Labels,
    eval_set: str,
    set_version: int,
    source_message_id: uuid.UUID,
    tags: list[str] | None = None,
) -> uuid.UUID:
    """Store a newly labeled email and return the row id."""
    values = {
        "eval_set": eval_set,
        "set_version": set_version,
        "source_message_id": source_message_id,
        "snapshot": snapshot.model_dump(mode="json"),
        "labels": labels.model_dump(mode="json"),
        "tags": tags or [],
    }
    stmt = insert(EvalLabel).values(**values).returning(EvalLabel.id)
    row_id: uuid.UUID = session.execute(stmt).scalar_one()
    return row_id


def retire(session: Session, label_id: uuid.UUID) -> None:
    """Take a current row out of its set. The row itself is kept."""
    stmt = (
        update(EvalLabel)
        .where(EvalLabel.id == label_id, EvalLabel.retired_at.is_(None))
        .values(retired_at=func.now())
        .returning(EvalLabel.id)
    )
    retired = session.execute(stmt).scalar_one_or_none()
    if retired is None:
        raise LabelError(f"eval label {label_id} is not current")


def correct(
    session: Session,
    label_id: uuid.UUID,
    labels: Labels,
    tags: list[str] | None = None,
) -> uuid.UUID:
    """Retire a row and store corrected labels for the same snapshot.

    Returns the new row id. `tags` defaults to the old row's tags.
    """
    old = session.get(EvalLabel, label_id)
    if old is None:
        raise LabelError(f"eval label {label_id} not found")
    retire(session, label_id)
    values = {
        "eval_set": old.eval_set,
        "set_version": old.set_version,
        "source_message_id": old.source_message_id,
        "snapshot": old.snapshot,
        "labels": labels.model_dump(mode="json"),
        "tags": old.tags if tags is None else tags,
        "corrects": old.id,
    }
    stmt = insert(EvalLabel).values(**values).returning(EvalLabel.id)
    row_id: uuid.UUID = session.execute(stmt).scalar_one()
    return row_id


def set_query(eval_set: str, set_version: int | None, as_of: datetime | None) -> Select[Any]:
    """Rows of a set that were current at `as_of`, or now when it is None."""
    cols = (
        EvalLabel.id,
        EvalLabel.eval_set,
        EvalLabel.snapshot,
        EvalLabel.labels,
        EvalLabel.tags,
    )
    query = select(*cols).where(EvalLabel.eval_set == eval_set)
    if set_version is not None:
        query = query.where(EvalLabel.set_version == set_version)
    if as_of is None:
        query = query.where(EvalLabel.retired_at.is_(None))
    else:
        live = EvalLabel.retired_at.is_(None) | (EvalLabel.retired_at > as_of)
        query = query.where(EvalLabel.created_at <= as_of, live)
    return query.order_by(EvalLabel.created_at, EvalLabel.id)


def sample_of(row_id: uuid.UUID, eval_set: str, data: dict[str, Any]) -> Sample:
    """A stored row as an evaluation sample; the id is made from the row id."""
    sample_id = f"{eval_set}-{row_id.hex}"
    return Sample.model_validate({"id": sample_id, **data})


def load_set(
    session: Session,
    eval_set: str,
    set_version: int | None = None,
    as_of: datetime | None = None,
) -> list[Sample]:
    """The samples of a set, read from the frozen snapshots only.

    `as_of` reads the set as it was at that time, so an earlier evaluation
    run can be explained after a correction.
    """
    query = set_query(eval_set, set_version, as_of)
    samples = []
    for row in session.execute(query):
        data = {"input": row.snapshot, "labels": row.labels, "tags": row.tags}
        samples.append(sample_of(row.id, row.eval_set, data))
    return samples
