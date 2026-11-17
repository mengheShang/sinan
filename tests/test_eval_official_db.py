"""The official test-set run against a throwaway database (Issue #87).

The model is a fake that answers with the synthetic set's recorded replies,
and Gmail is a reader that finds nothing. Every name and value is fictional.
"""

import uuid
from collections.abc import Callable, Iterator

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.db import labels, sampling
from app.db.models import AuditEvent, EvalRun
from app.extraction.prefilter import EXAMPLE_RULES, load_rules
from app.extraction.prompt import prepare
from app.providers.fakes import FakeExtractor, FakeLinkPicker
from app.providers.mail import MailReader
from evals import official, replies
from evals.dataset import Sample, SetError
from evals.official import Live
from evals.run import SETS, LiveLinks, LiveReply
from tests.conftest import alembic_config

pytestmark = pytest.mark.db

COMMIT = "0123456789abcdef0123456789abcdef01234567"
SAMPLES = SETS["synthetic"].load()
# Fixed: the parts of the report a replay must reproduce
SAME = ("counts", "metrics", "email_groups", "sender_trust")


@pytest.fixture
def session(conn: Connection) -> Iterator[Session]:
    with Session(bind=conn, join_transaction_mode="create_savepoint") as s:
        yield s


def draw_test(session: Session, samples: list[Sample], version: int = 1) -> None:
    """Label `samples` as the test set and record their picks, then commit."""
    for sample in samples:
        source = uuid.uuid4()
        labels.add(session, sample.input, sample.labels, "test", version, source)
        pick = {"eval_set": "test", "set_version": version, "role": "school", "stratum": "passed"}
        event = AuditEvent(
            event_type=sampling.PICK_EVENT,
            actor="user",
            subject_type="message",
            subject_id=source,
            data=pick,
        )
        session.add(event)
    # Releases this session's savepoint only; the test's connection rolls back
    session.commit()


def responses(samples: list[Sample]) -> dict[str, str]:
    """The synthetic recorded reply for each email's prepared text."""
    recorded = replies.load(SETS["synthetic"].replies)
    found = {}
    for sample in samples:
        snap = sample.input
        text = prepare(snap.subject, snap.from_addr, snap.sent_at, snap.body_text)
        if sample.id in recorded:
            found[text] = recorded[sample.id]
    return found


def no_reader(_: Session, __: uuid.UUID, ___: str) -> MailReader | None:
    return None


def live(answers: dict[str, str] | None = None, commit: str = COMMIT) -> Live:
    found = responses(SAMPLES) if answers is None else answers

    def reply() -> LiveReply:
        return LiveReply(FakeExtractor(found))

    def links() -> LiveLinks:
        return LiveLinks(FakeLinkPicker())

    return Live(load_rules(EXAMPLE_RULES), reply, links, no_reader, commit)


def runs(session: Session) -> int:
    count: int = session.execute(sa.select(sa.func.count()).select_from(EvalRun)).scalar_one()
    return count


# ---- Migration ----


def test_0008_follows_0007(db_url: str) -> None:
    script = ScriptDirectory.from_config(alembic_config(db_url))
    assert script.get_revision("0008").down_revision == "0007"


# ---- Loading the test set ----


def test_load_test_reads_the_newest_test_version(session: Session) -> None:
    draw_test(session, SAMPLES[:3])
    labels.add(session, SAMPLES[3].input, SAMPLES[3].labels, "dev", 1, uuid.uuid4())
    found = official.load_test(session)
    assert found.set_version == 1
    assert len(found.samples) == 3
    assert all(s.id.startswith("test-") for s in found.samples)
    assert set(found.sources) == {s.id for s in found.samples}


def test_load_test_without_a_draw_is_refused(session: Session) -> None:
    with pytest.raises(SetError, match="no test set"):
        official.load_test(session)


# ---- The recorded store round trip ----


def test_record_then_replay_gives_identical_numbers(session: Session) -> None:
    draw_test(session, SAMPLES)
    first = official.run_official(session, live())
    session.commit()
    assert runs(session) == 1
    again = official.replay(session, load_rules(EXAMPLE_RULES))
    for key in SAME:
        assert again[key] == first[key]
    assert again["linking"] | {"mode": None} == first["linking"] | {"mode": None}
    assert again["header"] == first["header"]
    assert (first["mode"], again["mode"]) == ("live", "recorded")
    assert first["metrics"]["item_recall"]["total"] > 0


def test_stored_run_names_what_it_depends_on(session: Session) -> None:
    draw_test(session, SAMPLES[:5])
    official.run_official(session, live())
    run = official.official(session, 1)
    assert run is not None
    assert (run.eval_set, run.set_version, run.commit) == ("test", 1, COMMIT)
    assert run.model == "fake-extractor"
    assert set(run.replies) <= {s.id for s in official.load_test(session).samples}


def test_replay_refuses_changed_rules(session: Session) -> None:
    draw_test(session, SAMPLES[:5])
    official.run_official(session, live())
    other = load_rules(EXAMPLE_RULES)
    changed = type(other)(other.allow, other.skip, other.sensitive_keywords, other.trust, False)
    with pytest.raises(SetError, match="rules differ"):
        official.replay(session, changed)


def test_replay_without_an_official_run_is_refused(session: Session) -> None:
    draw_test(session, SAMPLES[:2])
    with pytest.raises(SetError, match="no official run"):
        official.replay(session, load_rules(EXAMPLE_RULES))


# ---- A second official run ----


def test_second_official_run_is_refused(session: Session) -> None:
    draw_test(session, SAMPLES[:5])
    official.run_official(session, live())
    with pytest.raises(SetError, match="already has an official run"):
        official.run_official(session, live())
    assert runs(session) == 1


@pytest.mark.parametrize("commit", [official.UNKNOWN, COMMIT + "-dirty"])
def test_official_run_refuses_an_unclean_commit(session: Session, commit: str) -> None:
    draw_test(session, SAMPLES[:2])
    with pytest.raises(SetError):
        official.run_official(session, live(commit=commit))
    assert runs(session) == 0


def test_stored_runs_cannot_be_changed(session: Session) -> None:
    draw_test(session, SAMPLES[:2])
    official.run_official(session, live())
    session.commit()
    for stmt in (
        sa.update(EvalRun).values(model="other"),
        sa.delete(EvalRun),
    ):
        savepoint = session.begin_nested()
        with pytest.raises(sa.exc.IntegrityError):
            session.execute(stmt)
        savepoint.rollback()


# ---- Range runs ----


def varying() -> Callable[[], LiveReply]:
    """A model that finds everything on odd calls and nothing on even ones."""
    calls = {"n": 0}
    full = responses(SAMPLES)

    def reply() -> LiveReply:
        calls["n"] += 1
        found = full if calls["n"] % 2 else {}
        return LiveReply(FakeExtractor(found))

    return reply


def test_range_reports_min_and_max_and_stores_nothing(session: Session) -> None:
    draw_test(session, SAMPLES)
    official.run_official(session, live())
    session.commit()
    base = live()
    spread = Live(base.rules, varying(), base.links, base.reader_for, COMMIT)
    found = official.run_range(session, spread, 2)
    assert tuple(found) == official.RANGE_KEYS
    assert found["runs"] == 2
    recall = found["ranges"]["item_recall"]
    assert recall["min"] < recall["max"]
    assert runs(session) == 1


def test_range_needs_the_official_run_first(session: Session) -> None:
    draw_test(session, SAMPLES[:2])
    with pytest.raises(SetError, match="after the official run"):
        official.run_range(session, live(), 2)


def test_range_needs_a_positive_count(session: Session) -> None:
    draw_test(session, SAMPLES[:2])
    with pytest.raises(SetError):
        official.run_range(session, live(), 0)
