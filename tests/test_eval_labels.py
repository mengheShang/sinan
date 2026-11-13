"""Schema and helper tests for `eval_labels` (migration 0005).

These use the throwaway database fixtures from conftest.py and the
fictional synthetic set only.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.db import labels as store
from app.db.models import Account, EvalLabel, Message
from evals.dataset import Labels, Sample, load
from tests.conftest import alembic_config

pytestmark = pytest.mark.db

SAMPLES = load()
FIRST = SAMPLES[0]
OTHER = SAMPLES[1]
RECEIVED = datetime(2026, 11, 2, 17, 0, tzinfo=UTC)
PAST = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def session(conn: Connection) -> Iterator[Session]:
    """A session inside the test's rolled-back connection."""
    with Session(bind=conn, join_transaction_mode="create_savepoint") as s:
        yield s


def fails(session: Session, stmt: sa.Executable) -> None:
    """Assert the statement is rejected by a constraint or trigger."""
    savepoint = session.begin_nested()
    with pytest.raises((sa.exc.IntegrityError, sa.exc.InternalError)):
        session.execute(stmt)
    savepoint.rollback()


def row(sample: Sample = FIRST, **changes: Any) -> dict[str, Any]:
    values = {
        "eval_set": "dev",
        "set_version": 1,
        "source_message_id": uuid.uuid4(),
        "snapshot": sample.input.model_dump(mode="json"),
        "labels": sample.labels.model_dump(mode="json"),
        "tags": list(sample.tags),
    }
    values.update(changes)
    return values


def put(session: Session, **changes: Any) -> uuid.UUID:
    stmt = sa.insert(EvalLabel).values(**row(**changes)).returning(EvalLabel.id)
    key: uuid.UUID = session.execute(stmt).scalar_one()
    return key


def new_message(session: Session) -> uuid.UUID:
    email = f"student-{uuid.uuid4().hex[:8]}@example.edu"
    acct = Account(provider="gmail", email=email, role="school")
    session.add(acct)
    session.flush()
    msg = Message(
        account_id=acct.id,
        provider_msg_id="m1",
        received_at=RECEIVED,
        from_addr="rivera@example.edu",
        subject="CS 000: Homework 3 posted",
        body_text="Homework 3 is due Friday Nov 13 at 11:59pm PT.",
        headers={"List-Unsubscribe": "<mailto:leave@example.edu>", "To": "class@example.edu"},
    )
    session.add(msg)
    session.flush()
    return msg.id


def no_items() -> Labels:
    return Labels(items=[], safe_to_auto_read=True)


def test_no_drift_between_models_and_migrations(engine: Engine, db_url: str) -> None:
    command.check(alembic_config(db_url))


def test_added_sample_reads_back_unchanged(session: Session) -> None:
    store.add(session, FIRST.input, FIRST.labels, "dev", 1, uuid.uuid4(), list(FIRST.tags))
    [sample] = store.load_set(session, "dev")
    assert sample.input == FIRST.input
    assert sample.labels == FIRST.labels
    assert sample.tags == FIRST.tags
    assert sample.id.startswith("dev-")


@pytest.mark.parametrize("bad", ["train", "DEV", ""])
def test_set_is_checked(session: Session, bad: str) -> None:
    fails(session, sa.insert(EvalLabel).values(**row(eval_set=bad)))


def test_set_version_starts_at_one(session: Session) -> None:
    fails(session, sa.insert(EvalLabel).values(**row(set_version=0)))


def test_snapshot_needs_every_field(session: Session) -> None:
    snapshot = FIRST.input.model_dump(mode="json")
    del snapshot["headers"]
    fails(session, sa.insert(EvalLabel).values(**row(snapshot=snapshot)))
    fails(session, sa.insert(EvalLabel).values(**row(snapshot=["from_addr"])))


def test_snapshot_role_is_checked(session: Session) -> None:
    snapshot = {**FIRST.input.model_dump(mode="json"), "role": "boss"}
    fails(session, sa.insert(EvalLabel).values(**row(snapshot=snapshot)))


def test_labels_and_tags_shape_is_checked(session: Session) -> None:
    fails(session, sa.insert(EvalLabel).values(**row(labels={"items": []})))
    fails(session, sa.insert(EvalLabel).values(**row(tags={"course": True})))


def test_one_current_row_per_email(session: Session) -> None:
    source = uuid.uuid4()
    put(session, source_message_id=source)
    fails(session, sa.insert(EvalLabel).values(**row(source_message_id=source)))
    other_set = row(source_message_id=source, eval_set="test")
    fails(session, sa.insert(EvalLabel).values(**other_set))


def test_rows_cannot_be_edited(session: Session) -> None:
    label = put(session)
    where = EvalLabel.id == label
    edit = sa.update(EvalLabel).where(where).values(labels={"items": [], "safe_to_auto_read": True})
    fails(session, edit)
    fails(session, sa.update(EvalLabel).where(where).values(eval_set="test"))
    both = sa.update(EvalLabel).where(where).values(retired_at=PAST, set_version=2)
    fails(session, both)


def test_rows_cannot_be_deleted(session: Session) -> None:
    label = put(session)
    fails(session, sa.delete(EvalLabel).where(EvalLabel.id == label))
    fails(session, sa.text("TRUNCATE eval_labels"))


def test_retired_row_cannot_be_retired_twice(session: Session) -> None:
    label = put(session)
    store.retire(session, label)
    again = sa.update(EvalLabel).where(EvalLabel.id == label).values(retired_at=PAST)
    fails(session, again)
    with pytest.raises(store.LabelError):
        store.retire(session, label)
    assert store.load_set(session, "dev") == []


def test_retired_at_is_the_transaction_time(session: Session) -> None:
    label = put(session)
    retire = sa.update(EvalLabel).where(EvalLabel.id == label).values(retired_at=PAST)
    session.execute(retire)
    query = sa.select(EvalLabel.retired_at, sa.func.now()).where(EvalLabel.id == label)
    retired_at, now = session.execute(query).one()
    assert retired_at == now


def test_correction_keeps_the_old_row(session: Session) -> None:
    old = put(session)
    new = store.correct(session, old, no_items())
    first = session.get(EvalLabel, old)
    second = session.get(EvalLabel, new)
    assert first is not None
    assert second is not None
    assert first.retired_at is not None
    assert first.labels == FIRST.labels.model_dump(mode="json")
    assert second.corrects == old
    assert second.retired_at is None
    assert second.snapshot == first.snapshot
    assert second.source_message_id == first.source_message_id


def test_supplied_created_at_is_overwritten(session: Session) -> None:
    label = put(session, created_at=PAST)
    query = sa.select(EvalLabel.created_at, sa.func.now()).where(EvalLabel.id == label)
    created, now = session.execute(query).one()
    assert created == now
    assert created != PAST


def test_created_at_cannot_be_changed(session: Session) -> None:
    label = put(session)
    where = EvalLabel.id == label
    fails(session, sa.update(EvalLabel).where(where).values(created_at=PAST))


def test_correction_needs_the_old_row_retired(session: Session) -> None:
    old = put(session)
    copy = sa.select(EvalLabel).where(EvalLabel.id == old)
    source = session.execute(copy).scalar_one().source_message_id
    early = row(source_message_id=uuid.uuid4(), corrects=old)
    fails(session, sa.insert(EvalLabel).values(**early))
    store.retire(session, old)
    same = row(source_message_id=source, corrects=old)
    session.execute(sa.insert(EvalLabel).values(**same))


@pytest.mark.parametrize(
    "change",
    [
        {"snapshot": OTHER.input.model_dump(mode="json")},
        {"source_message_id": uuid.uuid4()},
        {"eval_set": "test"},
    ],
)
def test_correction_keeps_snapshot_source_and_set(session: Session, change: dict[str, Any]) -> None:
    source = uuid.uuid4()
    old = put(session, source_message_id=source)
    store.retire(session, old)
    values = {**row(source_message_id=source, corrects=old), **change}
    fails(session, sa.insert(EvalLabel).values(**values))


def test_a_row_is_corrected_once(session: Session) -> None:
    old = put(session)
    new = store.correct(session, old, no_items())
    store.retire(session, new)
    source = session.get(EvalLabel, old)
    assert source is not None
    twice = row(source_message_id=source.source_message_id, corrects=old)
    fails(session, sa.insert(EvalLabel).values(**twice))


def test_freeze_copies_only_extractor_inputs(session: Session) -> None:
    msg = new_message(session)
    snapshot = store.freeze(session, msg)
    assert snapshot.headers == {"List-Unsubscribe": "<mailto:leave@example.edu>"}
    assert snapshot.sent_at == RECEIVED
    assert snapshot.role == "school"


def test_freeze_needs_a_message(session: Session) -> None:
    with pytest.raises(store.LabelError):
        store.freeze(session, uuid.uuid4())


def test_changing_the_message_leaves_the_snapshot(session: Session) -> None:
    msg_id = new_message(session)
    snapshot = store.freeze(session, msg_id)
    labels = Labels(items=[], safe_to_auto_read=False)
    store.add(session, snapshot, labels, "test", 1, msg_id)

    msg = session.get(Message, msg_id)
    assert msg is not None
    msg.subject = "CS 000: Homework 3 moved"
    msg.body_text = "Homework 3 is now due Monday Nov 16 at 11:59pm PT."
    msg.headers = {}
    msg.sent_at = RECEIVED + timedelta(days=3)
    session.flush()

    [sample] = store.load_set(session, "test")
    assert sample.input == snapshot
    session.delete(msg)
    session.flush()
    [sample] = store.load_set(session, "test")
    assert sample.input == snapshot


def test_reads_filter_by_set_and_version(session: Session) -> None:
    put(session, eval_set="dev", set_version=1)
    put(session, sample=OTHER, eval_set="dev", set_version=2)
    put(session, eval_set="test", set_version=1)
    assert len(store.load_set(session, "dev")) == 2
    assert len(store.load_set(session, "dev", set_version=2)) == 1
    assert len(store.load_set(session, "test")) == 1


def test_downgrade_then_upgrade(engine: Engine, db_url: str) -> None:
    """Runs last in this module: it drops and recreates the table."""
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "0004")
    inspector = sa.inspect(engine)
    assert "eval_labels" not in inspector.get_table_names()
    assert "proposals" in inspector.get_table_names()
    with engine.connect() as c:
        query = "SELECT count(*) FROM pg_proc WHERE proname LIKE 'sinan_eval_label%'"
        assert c.execute(sa.text(query)).scalar_one() == 0
    command.upgrade(cfg, "head")
    inspector = sa.inspect(engine)
    assert "eval_labels" in inspector.get_table_names()
