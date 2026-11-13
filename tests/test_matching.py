"""Change matching and proposal validity: pure functions, fictional titles."""

from datetime import UTC, datetime, timedelta

import pytest

from app.items.matching import Known, course_key, match, title_words
from app.proposals import item_update, parse_utc, payload_hash, utc_text, validity

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def pool(*rows: tuple[str | None, str]) -> list[Known[int]]:
    return [Known(n, course, title) for n, (course, title) in enumerate(rows)]


# ---- Matching ----


@pytest.mark.parametrize("course", ["CS 000", "cs000", "CS-000", "ＣＳ ０００"])
def test_course_spellings_agree(course: str) -> None:
    assert course_key(course) == "cs000"


def test_blank_course_is_none() -> None:
    assert course_key(" - ") is None
    assert course_key(None) is None


def test_title_drops_the_course_words() -> None:
    assert title_words("MATH 005 midterm", "MATH 005") == {"midterm"}
    assert title_words("CS000 Homework 3", "CS 000") == {"homework", "3"}


def test_one_candidate() -> None:
    found = match("CS 000", "Homework 3", pool(("CS 000", "Homework 3"), ("CS 000", "Quiz 2")))
    assert (found.outcome, found.target) == ("one", 0)


def test_course_code_in_title_still_matches() -> None:
    found = match("MATH 005", "Midterm exam", pool(("MATH 005", "MATH 005 midterm")))
    assert found.outcome == "one"


def test_several_candidates() -> None:
    known = pool(("CS 000", "Homework 3"), ("CS 000", "Homework 3 written part"))
    found = match("CS 000", "Homework 3", known)
    assert (found.outcome, found.target, found.candidates) == ("several", None, 2)


def test_no_candidate() -> None:
    assert match("CS 000", "Homework 3", []).outcome == "none"


def test_numbers_must_agree() -> None:
    # Problem Set 4 and 5 share half their words, but are different items
    assert match("PHYS 003", "Problem Set 5", pool(("PHYS 003", "Problem Set 4"))).outcome == "none"


def test_course_must_agree() -> None:
    assert match("CS 001", "Homework 3", pool(("CS 000", "Homework 3"))).outcome == "none"


def test_no_course_matches_only_no_course() -> None:
    known = pool((None, "Pay December rent"), ("CS 000", "Pay December rent"))
    found = match(None, "Pay December rent", known)
    assert (found.outcome, found.target) == ("one", 0)


def test_unrelated_titles_do_not_match() -> None:
    assert match("CS 000", "Final exam", pool(("CS 000", "Final project"))).outcome == "none"


def test_title_of_only_course_words_matches_nothing() -> None:
    assert match("CS 000", "CS 000", pool(("CS 000", "CS 000"))).outcome == "none"


def test_non_latin_titles() -> None:
    found = match("数学 101", "期中考试", pool(("数学 101", "期中考试")))
    assert found.outcome == "one"


# ---- Validity ----


def test_valid_until_expiry() -> None:
    expires = NOW + timedelta(days=1)
    assert validity("pending", 1, 1, expires, NOW) == "valid"
    assert validity("pending", 1, 1, expires, expires) == "expired"


def test_newer_version_supersedes() -> None:
    assert validity("pending", 1, 2, NOW + timedelta(days=1), NOW) == "superseded"


def test_superseded_wins_over_expired() -> None:
    assert validity("pending", 1, 2, NOW, NOW) == "superseded"


@pytest.mark.parametrize("status", ["approved", "rejected", "expired", "superseded", "executed"])
def test_decided_stays_decided(status: str) -> None:
    assert validity(status, 1, 1, NOW + timedelta(days=1), NOW) == status


# ---- Payload ----


def test_utc_text_round_trip() -> None:
    when = datetime(2026, 11, 17, 7, 59, tzinfo=UTC)
    assert utc_text(when) == "2026-11-17T07:59:00Z"
    assert parse_utc(utc_text(when)) == when
    assert utc_text(None) is None


def test_utc_text_needs_a_zone() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_text(datetime(2026, 11, 17, 7, 59))


def test_item_update_payload_hashes() -> None:
    before = ("Friday 11:59pm PT", datetime(2026, 11, 14, 7, 59, tzinfo=UTC))
    after = ("Monday 11:59pm PT", datetime(2026, 11, 17, 7, 59, tzinfo=UTC))
    payload = item_update("item-1", before, after, "Homework 3 is now due Monday")
    assert payload["after"] == {
        "due_raw": "Monday 11:59pm PT",
        "due_at": "2026-11-17T07:59:00Z",
        "zone_assumed": False,
        "time_assumed": False,
    }
    assert len(payload_hash(payload)) == 64
