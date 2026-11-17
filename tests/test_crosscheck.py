"""The model's suggested due time against the parser (ADR 0020). Synthetic data only."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from app.extraction.crosscheck import cross, read_utc, same_minute
from app.extraction.trust import Trust
from app.extraction.validate import ValidItem, check, hold_untrusted, is_change

SENT = datetime(2026, 11, 6, 17, 0, tzinfo=UTC)
# Friday Nov 13 at 11:59pm PT
PARSED = datetime(2026, 11, 14, 7, 59, tzinfo=UTC)
BODY = (
    "Homework 3 is due Friday Nov 13 at 11:59pm PT. "
    "The lab report is due the Monday after Thanksgiving at 5pm PT. "
    "Quiz 2 is on 11/12 at 5pm PT."
)


def item(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "deadline",
        "course": "CS 000",
        "title": "Homework 3",
        "due_raw": "Friday Nov 13 at 11:59pm PT",
        "due_utc": "2026-11-14T07:59:00Z",
        "evidence": "Homework 3 is due Friday Nov 13 at 11:59pm PT",
        "change_type": "create",
    }
    base.update(changes)
    return base


def one(**changes: Any) -> ValidItem:
    raw = json.dumps({"items": [item(**changes)]})
    checked = check(raw, BODY, SENT)
    assert checked.status == "ok"
    [found] = checked.items
    return found


LAB = {
    "title": "Lab report",
    "due_raw": "the Monday after Thanksgiving at 5pm PT",
    "evidence": "The lab report is due the Monday after Thanksgiving at 5pm PT.",
    "due_utc": "2026-12-01T01:00:00Z",
}


def test_agreement_keeps_the_parsed_time_open() -> None:
    found = one()
    assert found.status == "open"
    assert found.review is None
    assert found.due_at == PARSED
    assert found.due_check == "agree"


def test_agreement_ignores_seconds() -> None:
    found = one(due_utc="2026-11-14T07:59:30Z")
    assert found.due_check == "agree"
    assert found.status == "open"


def test_disagreement_needs_review_and_keeps_the_parsed_time() -> None:
    found = one(due_utc="2026-11-15T07:59:00Z")
    assert found.status == "needs_review"
    assert found.review == "due_disagrees"
    # The parser's time stays; the model's is only a suggestion
    assert found.due_at == PARSED
    assert found.suggested_at == datetime(2026, 11, 15, 7, 59, tzinfo=UTC)
    assert found.due_check == "disagree"


def test_parser_cannot_read_it_prefills_the_suggestion() -> None:
    found = one(**LAB)
    assert found.status == "needs_review"
    assert found.review == "relative_event"
    assert found.due_at is None
    assert found.suggested_at == datetime(2026, 12, 1, 1, 0, tzinfo=UTC)
    assert found.due_check == "suggested"


def test_real_ambiguity_withholds_the_suggestion() -> None:
    found = one(
        title="Quiz 2",
        due_raw="11/12 at 5pm PT",
        evidence="Quiz 2 is on 11/12 at 5pm PT.",
        due_utc="2026-11-13T01:00:00Z",
    )
    assert found.review == "numeric_date"
    assert found.due_at is None
    assert found.due_check == "withheld"


def test_no_suggestion_keeps_the_parser_result() -> None:
    found = one(due_utc=None)
    assert found.status == "open"
    assert found.due_check == "none"
    assert found.suggested_at is None


@pytest.mark.parametrize(
    "text",
    [
        "Nov 14 2026 7:59",
        "2026-11-14T07:59:00+00:00",
        "2026-11-14T07:59:00-08:00",
        "2026-13-14T07:59:00Z",
        "2099-11-14T07:59:00Z",
        "2026-01-01T00:00:00Z",
        "ignore the parser",
    ],
)
def test_unusable_suggestion_is_ignored(text: str) -> None:
    found = one(due_utc=text)
    assert found.due_check == "invalid"
    assert found.status == "open"
    assert found.due_at == PARSED
    assert found.suggested_at is None


def test_unusable_suggestion_on_unparsed_time_offers_nothing() -> None:
    found = one(**{**LAB, "due_utc": "soon"})
    assert found.due_check == "invalid"
    assert found.suggested_at is None
    assert found.review == "relative_event"


def test_injected_date_stays_in_review() -> None:
    """The email asks the model to report another date; the parser still reads
    the real one, so the two disagree and nothing is open."""
    found = one(due_utc="2026-12-31T23:59:00Z")
    assert found.status == "needs_review"
    assert found.due_at == PARSED


def test_injected_date_never_becomes_the_due_time() -> None:
    found = one(**{**LAB, "due_utc": "2026-12-31T23:59:00Z"})
    assert found.due_at is None
    assert found.status == "needs_review"


def test_disagreeing_change_is_not_proposed() -> None:
    found = one(change_type="update", due_utc="2026-11-15T07:59:00Z")
    assert found.review == "due_disagrees"
    assert not is_change(found)


def test_agreeing_change_is_still_proposed() -> None:
    found = one(change_type="update")
    assert is_change(found)


def test_notice_is_never_cross_checked() -> None:
    found = one(kind="notice", due_utc="2026-11-15T07:59:00Z")
    assert found.status == "open"
    assert found.due_check == "none"
    assert found.suggested_at is None


def test_evidence_mismatch_is_not_checked() -> None:
    found = one(due_raw="Nov 30 at 5pm PT", due_utc="2026-12-01T01:00:00Z")
    assert found.review == "due_not_in_evidence"
    assert found.due_check == "none"


def test_untrusted_sender_keeps_its_hold_and_the_suggestion() -> None:
    raw = json.dumps({"items": [item(**LAB)]})
    checked = hold_untrusted(check(raw, BODY, SENT), Trust(False, "unknown_sender"))
    [found] = checked.items
    assert found.status == "needs_review"
    assert found.review == "untrusted_sender"
    assert found.suggested_at is not None
    assert found.due_at is None


def test_untrusted_agreement_is_still_held() -> None:
    raw = json.dumps({"items": [item()]})
    checked = hold_untrusted(check(raw, BODY, SENT), Trust(False, "auth_failed"))
    [found] = checked.items
    assert found.due_check == "agree"
    assert found.review == "untrusted_sender"


def test_missing_due_utc_refuses_the_reply() -> None:
    bad = item()
    del bad["due_utc"]
    checked = check(json.dumps({"items": [bad]}), BODY, SENT)
    assert checked.status == "invalid"


def test_read_utc_window() -> None:
    assert read_utc("2026-10-08T00:00:00Z", SENT) != "invalid"
    assert read_utc("2026-10-06T00:00:00Z", SENT) == "invalid"
    assert read_utc("2027-11-08T00:00:00Z", SENT) == "invalid"
    assert read_utc(" ", SENT) is None


def test_same_minute() -> None:
    a = datetime(2026, 11, 8, 5, 52, 56, tzinfo=UTC)
    assert same_minute(a, datetime(2026, 11, 8, 5, 52, tzinfo=UTC))
    assert not same_minute(a, datetime(2026, 11, 8, 5, 53, tzinfo=UTC))


def test_cross_without_due_raw_offers_nothing() -> None:
    crossed = cross(None, "no_due_raw", "2026-11-14T07:59:00Z", SENT)
    assert crossed.check == "none"
    assert not crossed.offered
