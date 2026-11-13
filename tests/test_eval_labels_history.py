"""Reading an eval set as it was, across real committed transactions.

`created_at` and `retired_at` are set by triggers to the transaction time,
so these tests commit each step in its own transaction and read the stored
times back. Rows cannot be deleted, so this module keeps its own throwaway
database (the `db_url` fixture is per module). Fictional data only.
"""

import uuid
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db import labels as store
from app.db.models import EvalLabel
from evals.dataset import Labels, load

pytestmark = pytest.mark.db

FIRST = load()[0]
TICK = timedelta(microseconds=1)


def stored_times(engine: Engine, label_id: uuid.UUID) -> tuple[datetime, datetime | None]:
    with Session(engine) as session:
        query = sa.select(EvalLabel.created_at, EvalLabel.retired_at)
        row = session.execute(query.where(EvalLabel.id == label_id)).one()
    return row.created_at, row.retired_at


def test_as_of_reads_the_set_before_and_after_a_correction(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        old = store.add(session, FIRST.input, FIRST.labels, "dev", 1, uuid.uuid4())
    fixed = Labels(items=[], safe_to_auto_read=True)
    with Session(engine) as session, session.begin():
        new = store.correct(session, old, fixed)

    old_created, old_retired = stored_times(engine, old)
    new_created, new_retired = stored_times(engine, new)
    assert old_retired is not None
    assert new_retired is None
    assert old_created < old_retired
    # Retiring and replacing happen in one transaction, so at one time
    assert old_retired == new_created

    with Session(engine) as session:
        assert store.load_set(session, "dev", as_of=old_created - TICK) == []
        [first] = store.load_set(session, "dev", as_of=old_created)
        [still] = store.load_set(session, "dev", as_of=new_created - TICK)
        [after] = store.load_set(session, "dev", as_of=new_created)
        [now] = store.load_set(session, "dev")
    assert first.labels == FIRST.labels
    assert still.labels == FIRST.labels
    assert after.labels == fixed
    assert now.labels == fixed
    assert first.input == after.input
