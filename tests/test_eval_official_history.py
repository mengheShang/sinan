"""Replaying the official run after a label correction, across real transactions.

`created_at` and `retired_at` are set to the transaction time, so each step
commits on its own. Rows cannot be deleted, so this module keeps its own
throwaway database (the `db_url` fixture is per module). Fictional data only.
"""

import uuid

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db import labels
from app.db.label_format import Labels
from app.extraction.prefilter import EXAMPLE_RULES, load_rules
from evals import official
from tests.test_eval_official_db import SAMPLES, draw_test, live

pytestmark = pytest.mark.db


def test_replay_reads_labels_as_the_official_run_did(engine: Engine) -> None:
    with Session(engine) as session:
        draw_test(session, SAMPLES)
    with Session(engine) as session:
        first = official.run_official(session, live())
        session.commit()
    with Session(engine) as session, session.begin():
        found = official.load_test(session).samples
        labelled = [s for s in found if s.labels.items]
        row = uuid.UUID(labelled[0].id.removeprefix("test-"))
        labels.correct(session, row, Labels(items=[], safe_to_auto_read=True))
    with Session(engine) as session:
        now = official.load_test(session).samples
        again = official.replay(session, load_rules(EXAMPLE_RULES))
    # The correction is current, but the replay reads the labels of the run
    assert sum(len(s.labels.items) for s in now) < sum(len(s.labels.items) for s in found)
    assert again["counts"] == first["counts"]
    assert again["metrics"] == first["metrics"]


def test_replace_official_makes_the_newer_run_official(engine: Engine) -> None:
    with Session(engine) as session:
        if official.official(session, 1) is None:
            draw_test(session, SAMPLES)
            official.run_official(session, live())
            session.commit()
    with Session(engine, expire_on_commit=False) as session:
        first = official.official(session, 1)
    assert first is not None
    with Session(engine) as session:
        official.run_official(session, live({}), replace=True)
        session.commit()
    with Session(engine, expire_on_commit=False) as session:
        newer = official.official(session, 1)
    assert newer is not None
    # The newer run, whose fake found nothing, is the official one
    assert newer.id != first.id
    assert newer.created_at > first.created_at
    assert newer.replies != first.replies
