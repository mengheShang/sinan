"""Drawing the development and test sets (Issue #41) from fictional synthetic mail."""

import uuid
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.db import labeling, sampling
from app.db.boost import Senders
from app.db.label_format import Labels
from app.db.labels import add, freeze
from app.db.models import Account, AuditEvent, EvalLabel, Message
from app.db.sampling import Candidate, Pick
from app.extraction.prefilter import Rules
from apps.worker.sample import lines_of, short_lines
from evals import real

# A marker that must never show up in an audit event or the printed output
MARKER = "Quokka-marker-5512"
SUBJECT = "Synthetic notice"
SENDER_DOMAIN = "example.edu"
# No sender rules: only the bulk header makes the prefilter skip
RULES = Rules()
START = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


# ---- Pure draw ----


def fake_pool(passed: int, skipped: int) -> list[Candidate]:
    found = []
    for role in sampling.ROLES:
        for stratum, n in (("passed", passed), ("skipped", skipped)):
            found += [Candidate(uuid.uuid4(), role, stratum) for _ in range(n)]
    return found


def mix(picks: list[Pick], eval_set: str) -> Counter[tuple[str, str]]:
    return Counter((p.role, p.stratum) for p in picks if p.eval_set == eval_set)


def test_quotas_follow_the_approved_mix() -> None:
    dev = sampling.quotas(100)
    test = sampling.quotas(50)
    assert dev == {
        ("school", "skipped"): 10,
        ("school", "passed"): 40,
        ("personal", "skipped"): 10,
        ("personal", "passed"): 40,
    }
    assert test == {
        ("school", "skipped"): 5,
        ("school", "passed"): 20,
        ("personal", "skipped"): 5,
        ("personal", "passed"): 20,
    }


def test_same_seed_same_draw() -> None:
    pool = fake_pool(80, 20)
    first = sampling.draw(pool, 7, 1, ("dev", "test"))
    again = sampling.draw(list(reversed(pool)), 7, 1, ("dev", "test"))
    # Input order does not matter: each stratum is sorted before shuffling
    assert first == again
    other = sampling.draw(pool, 8, 1, ("dev", "test"))
    assert {p.message_id for p in other} != {p.message_id for p in first}


def test_sets_do_not_overlap_and_keep_the_mix() -> None:
    picks = sampling.draw(fake_pool(80, 20), 7, 1, ("dev", "test"))
    dev = {p.message_id for p in picks if p.eval_set == "dev"}
    test = {p.message_id for p in picks if p.eval_set == "test"}
    assert len(dev) == 100
    assert len(test) == 50
    assert not dev & test
    assert mix(picks, "dev") == Counter(sampling.quotas(100))
    assert mix(picks, "test") == Counter(sampling.quotas(50))


def test_shortfall_reports_every_stratum_and_draws_nothing() -> None:
    pool = fake_pool(80, 20)
    # Drop personal skipped mail to 3 and school skipped mail to 14
    pool = [c for c in pool if c.stratum != "skipped"]
    pool += [Candidate(uuid.uuid4(), "personal", "skipped") for _ in range(3)]
    pool += [Candidate(uuid.uuid4(), "school", "skipped") for _ in range(14)]
    with pytest.raises(sampling.Short) as err:
        sampling.draw(pool, 7, 1, ("dev", "test"))
    found = {(s.role, s.stratum): (s.needed, s.available) for s in err.value.found}
    assert found == {("personal", "skipped"): (15, 3), ("school", "skipped"): (15, 14)}
    text = "\n".join(short_lines(err.value))
    assert "personal/skipped: need 15, have 3" in text


# ---- On the database ----


@pytest.fixture
def session(conn: Connection) -> Iterator[Session]:
    with Session(bind=conn, join_transaction_mode="create_savepoint") as s:
        yield s


def account(session: Session, role: str) -> Account:
    acct = Account(provider="gmail", email=f"{role}@example.edu", role=role)
    session.add(acct)
    session.flush()
    return acct


def message(session: Session, acct: Account, n: int, bulk: bool, body: str | None = None) -> None:
    headers = {"To": "student@example.edu"}
    if bulk:
        headers["List-Unsubscribe"] = "<mailto:leave@example.org>"
    when = START + timedelta(hours=n)
    msg = Message(
        account_id=acct.id,
        provider_msg_id=f"syn-{acct.role}-{n}",
        received_at=when,
        sent_at=when,
        from_addr=f"sender{n}@{SENDER_DOMAIN}",
        subject=f"{SUBJECT} {n}",
        body_text=f"Body {n}. {MARKER}" if body is None else body,
        headers=headers,
    )
    session.add(msg)


def fill(session: Session, passed: int, skipped: int, offset: int = 0) -> None:
    """`passed` and `skipped` messages per role, all fictional."""
    for role in sampling.ROLES:
        acct = account(session, role) if offset == 0 else existing(session, role)
        for n in range(passed + skipped):
            message(session, acct, offset + n, bulk=n >= passed)
    session.flush()


def existing(session: Session, role: str) -> Account:
    query = sa.select(Account).where(Account.role == role)
    found: Account = session.execute(query).scalar_one()
    return found


def events(session: Session, kind: str) -> list[AuditEvent]:
    query = sa.select(AuditEvent).where(AuditEvent.event_type == kind)
    return list(session.execute(query).scalars())


def run(
    session: Session, seed: int = 41, mode: sampling.Mode = "first", write: bool = True
) -> sampling.Result:
    return sampling.run(session, RULES, seed, mode, write, "example")


def test_dry_run_records_nothing_and_repeats(session: Session) -> None:
    fill(session, 70, 20)
    first = run(session, write=False)
    again = run(session, write=False)
    assert first.picks == again.picks
    assert len(first.picks) == 150
    assert events(session, sampling.DRAW_EVENT) == []
    assert events(session, sampling.PICK_EVENT) == []


def test_write_records_the_draw_with_the_mix(session: Session) -> None:
    fill(session, 70, 20)
    result = run(session)
    picks = events(session, sampling.PICK_EVENT)
    assert len(picks) == 150
    sets = Counter(e.data["eval_set"] for e in picks)
    assert sets == {"dev": 100, "test": 50}
    assert {e.data["set_version"] for e in picks} == {1}
    assert len({e.subject_id for e in picks}) == 150
    dev_mix = Counter(
        (e.data["role"], e.data["stratum"]) for e in picks if e.data["eval_set"] == "dev"
    )
    assert dev_mix == Counter(sampling.quotas(100))
    # The prefilter really put the bulk mail in the skipped stratum
    for event in picks:
        msg = session.get(Message, event.subject_id)
        assert msg is not None
        bulk = "List-Unsubscribe" in msg.headers
        assert (event.data["stratum"] == "skipped") == bulk
    (draw,) = events(session, sampling.DRAW_EVENT)
    assert draw.data["seed"] == 41
    assert draw.data["counts"] == result.counts()
    assert draw.data["counts"]["test"]["school/skipped"] == 5


def test_nothing_in_events_or_output_holds_content(session: Session) -> None:
    fill(session, 70, 20)
    result = run(session)
    printed = "\n".join(lines_of(result, "example"))
    texts = [printed] + [repr(e.data) for e in session.execute(sa.select(AuditEvent)).scalars()]
    for text in texts:
        assert MARKER not in text
        assert SUBJECT not in text
        assert SENDER_DOMAIN not in text
    assert "dev: 100" in printed
    assert "test: 50" in printed


def test_same_seed_same_draw_on_the_database(session: Session) -> None:
    fill(session, 70, 20)
    a = run(session, seed=5, write=False)
    b = run(session, seed=5, write=False)
    c = run(session, seed=6, write=False)
    assert a.picks == b.picks
    assert {p.message_id for p in a.picks} != {p.message_id for p in c.picks}


def test_unlabelable_mail_is_left_out(session: Session) -> None:
    fill(session, 70, 20)
    acct = existing(session, "school")
    message(session, acct, 900, bulk=False, body="")
    session.flush()
    result = run(session, write=False)
    assert result.pool.unlabelable == 1
    assert len(result.pool.candidates) == 180


def test_shortfall_records_nothing(session: Session) -> None:
    # 14 skipped emails per role, 15 needed
    fill(session, 70, 14)
    with pytest.raises(sampling.Short) as err:
        run(session)
    assert {(s.role, s.stratum) for s in err.value.found} == {
        ("school", "skipped"),
        ("personal", "skipped"),
    }
    assert events(session, sampling.PICK_EVENT) == []
    assert events(session, sampling.DRAW_EVENT) == []


def test_the_test_set_cannot_be_drawn_again(session: Session) -> None:
    fill(session, 70, 20)
    run(session)
    test_ids = {
        e.subject_id for e in events(session, sampling.PICK_EVENT) if e.data["eval_set"] == "test"
    }
    for seed in (41, 42):
        with pytest.raises(sampling.DrawError, match="test set is fixed"):
            run(session, seed=seed)
        with pytest.raises(sampling.DrawError, match="test set is fixed"):
            run(session, seed=seed, write=False)
    assert len(events(session, sampling.DRAW_EVENT)) == 1
    after = {
        e.subject_id for e in events(session, sampling.PICK_EVENT) if e.data["eval_set"] == "test"
    }
    assert after == test_ids


def test_redraw_dev_bumps_the_version_and_keeps_test(session: Session) -> None:
    fill(session, 70, 20)
    run(session)
    first = {e.subject_id for e in events(session, sampling.PICK_EVENT)}
    # More mail syncs; the new dev draw takes only mail outside earlier draws
    fill(session, 50, 15, offset=1000)
    result = run(session, seed=99, mode="redraw-dev")
    assert result.version == 2
    assert result.sets == ("dev",)
    assert result.pool.drawn_before == 150
    new = [e for e in events(session, sampling.PICK_EVENT) if e.data["set_version"] == 2]
    assert len(new) == 100
    assert {e.data["eval_set"] for e in new} == {"dev"}
    assert not {e.subject_id for e in new} & first
    assert sampling.latest_versions(session) == {"dev": 2, "test": 1}


def test_redraw_dev_before_any_draw_is_refused(session: Session) -> None:
    fill(session, 70, 20)
    with pytest.raises(sampling.DrawError, match="nothing drawn yet"):
        run(session, mode="redraw-dev")


def test_labels_outside_a_draw_block_the_first_draw(session: Session) -> None:
    fill(session, 70, 20)
    some = session.execute(sa.select(Message.id).limit(1)).scalar_one()
    labels = Labels.model_validate({"items": [], "safe_to_auto_read": True})
    add(session, freeze(session, some), labels, "dev", 1, some)
    with pytest.raises(sampling.DrawError, match="outside any draw"):
        run(session)
    assert events(session, sampling.PICK_EVENT) == []


def test_queue_shows_only_sampled_mail(session: Session) -> None:
    fill(session, 70, 20)
    run(session)
    counts = labeling.counts(session)
    assert (counts.dev_total, counts.test_total, counts.remaining) == (100, 50, 150)
    picked = {e.subject_id: e.data["eval_set"] for e in events(session, sampling.PICK_EVENT)}
    # Walk the whole queue: every email is sampled, dev before test
    seen = []
    current = labeling.first_id(session)
    while current is not None:
        seen.append(current)
        current = labeling.first_id(session, after=labeling.position(session, current))
    assert set(seen) == set(picked)
    sets = [picked[m] for m in seen]
    assert sets == ["dev"] * 100 + ["test"] * 50
    unsampled = session.execute(sa.select(Message.id).where(Message.id.not_in(picked))).scalars()
    for message_id in unsampled:
        with pytest.raises(labeling.UnknownEmail):
            labeling.read_email(session, message_id)


# ---- The supplementary dev draw (Issue #78) ----

BOOST = sampling.Boost(Senders(), "example")
NO_ITEMS = Labels.model_validate({"items": [], "safe_to_auto_read": True})


def fill_likely(session: Session, n: int, offset: int = 5000) -> set[uuid.UUID]:
    """`n` messages whose body names a due date, alternating roles."""
    for i in range(n):
        acct = existing(session, sampling.ROLES[i % 2])
        body = f"Lab {i} is due Friday. {MARKER}"
        message(session, acct, offset + i, bulk=i % 4 == 0, body=body)
    session.flush()
    names = [f"syn-{sampling.ROLES[i % 2]}-{offset + i}" for i in range(n)]
    query = sa.select(Message.id).where(Message.provider_msg_id.in_(names))
    return set(session.execute(query).scalars())


def boost_run(session: Session, seed: int = 41, write: bool = True) -> sampling.Result:
    return sampling.run(session, RULES, seed, "boost-dev", write, "example", BOOST)


def picks_of(session: Session) -> list[AuditEvent]:
    return events(session, sampling.PICK_EVENT)


def test_boost_dry_run_repeats_and_records_nothing(session: Session) -> None:
    fill(session, 70, 20)
    run(session)
    fill_likely(session, 50)
    a = boost_run(session, seed=5, write=False)
    b = boost_run(session, seed=5, write=False)
    c = boost_run(session, seed=6, write=False)
    assert a.picks == b.picks
    assert {p.message_id for p in a.picks} != {p.message_id for p in c.picks}
    assert len(events(session, sampling.DRAW_EVENT)) == 1
    assert len(picks_of(session)) == 150


def test_boost_joins_the_current_dev_version(session: Session) -> None:
    fill(session, 70, 20)
    run(session)
    before = {e.subject_id: dict(e.data) for e in picks_of(session)}
    likely = fill_likely(session, 50)
    result = boost_run(session)
    assert (result.version, result.sets) == (1, ("dev",))
    new = [e for e in picks_of(session) if e.subject_id not in before]
    assert len(new) == 40
    assert {e.subject_id for e in new} <= likely
    for e in new:
        assert (e.data["eval_set"], e.data["set_version"]) == ("dev", 1)
        assert e.data["stratum"] == sampling.BOOST
        msg = session.get(Message, e.subject_id)
        assert msg is not None
        bulk = "List-Unsubscribe" in msg.headers
        assert e.data["prefilter"] == ("skipped" if bulk else "passed")
    # The earlier picks are unchanged, the test set with them
    after = {e.subject_id: dict(e.data) for e in picks_of(session) if e.subject_id in before}
    assert after == before
    assert sampling.latest_versions(session) == {"dev": 1, "test": 1}
    draws = events(session, sampling.DRAW_EVENT)
    boosts = [d for d in draws if d.data.get("stratum") == sampling.BOOST]
    assert len(boosts) == 1
    assert boosts[0].data["sets"] == ["dev"]
    assert boosts[0].data["senders"] == "example"
    assert boosts[0].data["counts"] == result.counts()


def test_boost_never_redraws_drawn_mail(session: Session) -> None:
    fill(session, 70, 20)
    # Every email names a deadline, the 150 in the first draw too
    session.execute(sa.update(Message).values(body_text=f"Report due Friday. {MARKER}"))
    run(session)
    drawn = {e.subject_id for e in picks_of(session)}
    # Only 30 undrawn emails remain: a shortfall, though all are likely
    with pytest.raises(sampling.Short) as err:
        boost_run(session)
    (short,) = err.value.found
    assert (short.needed, short.available) == (40, 30)
    fill_likely(session, 10)
    result = boost_run(session)
    assert result.pool.drawn_before == 150
    assert not {p.message_id for p in result.picks} & drawn


def test_boost_shortfall_records_nothing(session: Session) -> None:
    fill(session, 70, 20)
    run(session)
    fill_likely(session, 39)
    with pytest.raises(sampling.Short):
        boost_run(session)
    assert len(picks_of(session)) == 150
    assert len(events(session, sampling.DRAW_EVENT)) == 1


def test_one_boost_per_dev_version(session: Session) -> None:
    fill(session, 70, 20)
    run(session)
    fill_likely(session, 90)
    boost_run(session)
    for write in (True, False):
        with pytest.raises(sampling.DrawError, match="already has its supplement"):
            boost_run(session, seed=42, write=write)
    # A new dev set version may get its own supplement, from undrawn mail only
    fill(session, 50, 15, offset=1000)
    run(session, seed=99, mode="redraw-dev")
    fill_likely(session, 40, offset=8000)
    drawn = {e.subject_id for e in picks_of(session)}
    result = boost_run(session)
    assert result.version == 2
    assert not {p.message_id for p in result.picks} & drawn


def test_boost_before_any_draw_is_refused(session: Session) -> None:
    fill(session, 70, 20)
    fill_likely(session, 50)
    with pytest.raises(sampling.DrawError, match="nothing drawn yet"):
        boost_run(session)


def test_boost_output_holds_counts_only(session: Session) -> None:
    fill(session, 70, 20)
    run(session)
    fill_likely(session, 50)
    result = boost_run(session)
    printed = "\n".join(lines_of(result, "example", "example"))
    texts = [printed] + [repr(e.data) for e in session.execute(sa.select(AuditEvent)).scalars()]
    for text in texts:
        assert MARKER not in text
        assert SUBJECT not in text
        assert SENDER_DOMAIN not in text
        assert "due Friday" not in text
    assert "boost senders: example" in printed
    assert f"stratum: {sampling.BOOST}" in printed
    assert "dev: 40" in printed
    assert f"available school/{sampling.BOOST}: 25" in printed
    short = sampling.Short([sampling.Shortfall("any", sampling.BOOST, 40, 3)])
    assert short_lines(short)[1] == f"  any/{sampling.BOOST}: need 40, have 3"


def walk(session: Session) -> list[uuid.UUID]:
    seen = []
    current = labeling.first_id(session)
    while current is not None:
        seen.append(current)
        current = labeling.first_id(session, after=labeling.position(session, current))
    return seen


def test_queue_lists_the_dev_draw_and_its_supplement(session: Session) -> None:
    fill(session, 70, 20)
    run(session)
    fill_likely(session, 50)
    boost_run(session)
    counts = labeling.counts(session)
    assert (counts.dev_total, counts.test_total, counts.remaining) == (140, 50, 190)
    picked = {e.subject_id: e.data for e in picks_of(session)}
    seen = walk(session)
    assert set(seen) == set(picked)
    sets = [picked[m]["eval_set"] for m in seen]
    assert sets == ["dev"] * 140 + ["test"] * 50
    boosted = [m for m in seen if picked[m]["stratum"] == sampling.BOOST]
    assert len(boosted) == 40
    # A supplement email is labeled into the dev set version it joined
    label_id = labeling.save(session, boosted[0], NO_ITEMS, [])
    row = session.get(EvalLabel, label_id)
    assert row is not None
    assert (row.eval_set, row.set_version) == ("dev", 1)


def test_real_dev_set_splits_out_the_supplement(session: Session) -> None:
    fill(session, 70, 20)
    run(session)
    fill_likely(session, 50)
    boost_run(session)
    picked = {e.subject_id: e.data for e in picks_of(session) if e.subject_id is not None}
    dev = [m for m, d in picked.items() if d["eval_set"] == "dev"]
    boosted = [m for m in dev if picked[m]["stratum"] == sampling.BOOST]
    plain = [m for m in dev if picked[m]["stratum"] != sampling.BOOST]
    test = [m for m, d in picked.items() if d["eval_set"] == "test"]
    for message_id in boosted[:3] + plain[:2] + test[:1]:
        labeling.save(session, message_id, NO_ITEMS, [])
    found = real.load_dev(session)
    assert found.set_version == 1
    assert len(found.samples) == 5
    assert Counter(found.parts.values()) == {real.BOOST: 3, real.RANDOM: 2}
    assert set(found.parts) == {s.id for s in found.samples}
