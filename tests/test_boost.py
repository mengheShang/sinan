"""Choosing mail likely to hold a deadline (Issue #78) on fictional text, and the pure draw."""

import uuid
from collections import Counter
from pathlib import Path

import pytest

from app.db import boost, sampling
from app.db.boost import Senders
from app.db.label_format import Snapshot
from app.db.sampling import Candidate
from app.extraction.prefilter import RulesError
from apps.worker.sample import mode_of, parse_args


def snap(
    subject: str = "Hello", body: str = "Nothing to see.", sender: str = "a@example.org"
) -> Snapshot:
    return Snapshot.model_validate(
        {
            "from_addr": sender,
            "subject": subject,
            "body_text": body,
            "sent_at": "2026-11-02T17:00:00Z",
            "role": "school",
            "headers": {},
        }
    )


NONE = Senders()


# ---- Keywords ----


@pytest.mark.parametrize(
    "text",
    [
        "Homework 4 is DUE Friday",
        "The deadline moved",
        "Two deadlines this week",
        "Please submit the form",
        "I submitted it",
        "Midterm review",
        "Finals week schedule",
        "Quizzes are open",
        "New assignment posted",
        "Group projects",
        "Exam room change",
        "Register by Nov 20",
        "Send it by Friday, please",
        "reply by thu",
        "no later than noon",
        "No  later\nthan Monday",
    ],
)
def test_keywords_hit(text: str) -> None:
    assert boost.keyword_hit(text)


@pytest.mark.parametrize(
    "text",
    [
        "Finally, some good news",
        "The projector is broken",
        "Examine the results",
        "Overdue library books",
        "Register for the fair",
        "Stop by the office",
        "A weekly newsletter",
        "",
    ],
)
def test_keywords_miss(text: str) -> None:
    assert not boost.keyword_hit(text)


def test_subject_or_body_counts() -> None:
    assert boost.likely_deadline(snap(subject="Quiz 2"), NONE)
    assert boost.likely_deadline(snap(body="Lab report due Monday."), NONE)
    assert not boost.likely_deadline(snap(), NONE)


# ---- Senders ----


def test_senders_match_addresses_and_subdomains() -> None:
    senders = Senders(frozenset({"prof.rivera@example.edu"}), frozenset({"lms.example.edu"}))
    assert boost.likely_deadline(snap(sender="Prof.Rivera@example.edu"), senders)
    assert boost.likely_deadline(snap(sender="notify@mail.lms.example.edu"), senders)
    assert not boost.likely_deadline(snap(sender="notify@example.edu"), senders)
    assert not boost.likely_deadline(snap(sender="x@notlms.example.edu"), senders)


def test_example_senders_load_and_are_fictional() -> None:
    senders = boost.load_senders(boost.EXAMPLE_SENDERS)
    assert senders.addresses
    assert senders.domains
    for value in senders.addresses | senders.domains:
        assert value.endswith(("example.edu", "example.com", "example.org"))


def test_missing_local_list_falls_back_to_the_example(tmp_path: Path) -> None:
    absent = tmp_path / "deadline_boost.toml"
    assert boost.senders_path(absent) == boost.EXAMPLE_SENDERS
    absent.write_text("[senders]\naddresses = []\n", encoding="utf-8")
    assert boost.senders_path(absent) == absent
    assert boost.load_senders(absent) == Senders()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"other": {}}, "unknown sections"),
        ({"senders": {"emails": []}}, "unknown keys"),
        ({"senders": {"addresses": "a@example.edu"}}, "must be a list"),
        ({"senders": {"addresses": ["not-an-address"]}}, "full addresses"),
        ({"senders": {"domains": ["@example.edu"]}}, "domains must look like"),
    ],
)
def test_bad_sender_list_names_the_key_only(data: dict[str, object], message: str) -> None:
    with pytest.raises(RulesError, match=message) as err:
        boost.parse_senders(data)
    assert "not-an-address" not in str(err.value)


# ---- The pure boost draw ----


def pool(hits: int, misses: int) -> list[Candidate]:
    found = []
    for n in range(hits + misses):
        role = sampling.ROLES[n % 2]
        stratum = "skipped" if n % 5 == 0 else "passed"
        found.append(Candidate(uuid.uuid4(), role, stratum, n < hits))
    return found


def test_boost_draw_is_deterministic_for_a_seed() -> None:
    candidates = pool(100, 50)
    first = sampling.draw_boost(candidates, 7, 1, 40)
    again = sampling.draw_boost(list(reversed(candidates)), 7, 1, 40)
    other = sampling.draw_boost(candidates, 8, 1, 40)
    assert first == again
    assert {p.message_id for p in first} != {p.message_id for p in other}


def test_boost_draw_takes_only_likely_mail_with_no_role_split() -> None:
    candidates = pool(60, 200)
    likely = {c.id for c in candidates if c.likely}
    picks = sampling.draw_boost(candidates, 7, 1, 40)
    assert len(picks) == 40
    assert len({p.message_id for p in picks}) == 40
    assert {p.message_id for p in picks} <= likely
    assert {(p.eval_set, p.stratum) for p in picks} == {("dev", sampling.BOOST)}
    # Every pick keeps its role and prefilter outcome
    by_id = {c.id: c for c in candidates}
    for p in picks:
        assert p.role == by_id[p.message_id].role
        assert p.prefilter == by_id[p.message_id].stratum


def test_boost_shortfall_draws_nothing() -> None:
    with pytest.raises(sampling.Short) as err:
        sampling.draw_boost(pool(39, 500), 7, 1, 40)
    (short,) = err.value.found
    assert (short.stratum, short.needed, short.available) == (sampling.BOOST, 40, 39)


def test_boost_draw_with_exactly_enough() -> None:
    picks = sampling.draw_boost(pool(40, 0), 7, 1, 40)
    assert Counter(p.stratum for p in picks) == {sampling.BOOST: 40}


# ---- The command line ----


def test_boost_flag_picks_the_boost_mode() -> None:
    args = parse_args(["--seed", "1", "--boost-dev", "40"])
    assert args.boost_dev == 40
    assert args.write is False
    assert mode_of(args) == "boost-dev"
    assert mode_of(parse_args(["--seed", "1"])) == "first"


@pytest.mark.parametrize(
    "flags",
    [
        ["--boost-dev", "40", "--redraw-dev"],
        ["--boost-dev", "0"],
        ["--boost-dev", "-1"],
    ],
)
def test_bad_boost_flags_are_refused(flags: list[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(["--seed", "1", *flags])
