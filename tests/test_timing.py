"""Item phases on a fixed clock (ADR 0015), without a database."""

from datetime import UTC, datetime, timedelta

import pytest

from app.items.timing import Phase, is_held, phase

NOW = datetime(2026, 11, 11, 15, 30, tzinfo=UTC)
DAY = timedelta(days=1)


@pytest.mark.parametrize(
    ("due", "expected"),
    [
        (NOW + DAY, "current"),
        (NOW, "current"),
        (NOW - DAY, "overdue"),
        (NOW - 2 * DAY + timedelta(minutes=1), "overdue"),
        (NOW - 2 * DAY, "past"),
        (NOW - 3 * DAY, "past"),
    ],
)
def test_due_time(due: datetime, expected: Phase) -> None:
    assert phase("open", due, NOW, NOW, held=False) == expected


def test_due_time_applies_to_review_items_too() -> None:
    assert phase("needs_review", NOW - 3 * DAY, NOW, NOW, held=False) == "past"


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (13 * DAY, "current"),
        (14 * DAY, "current"),
        (15 * DAY, "stale"),
    ],
)
def test_review_item_without_due_time_goes_stale(age: timedelta, expected: Phase) -> None:
    assert phase("needs_review", None, NOW - age, NOW, held=False) == expected


def test_open_item_without_due_time_never_goes_stale() -> None:
    assert phase("open", None, NOW - 60 * DAY, NOW, held=False) == "current"


@pytest.mark.parametrize(
    ("status", "review", "pending", "expected"),
    [
        ("open", None, True, True),
        ("needs_review", "proposal_expired", False, True),
        ("needs_review", "no_zone", False, False),
        # Reopened by the owner (done, then open): the old code no longer holds it
        ("open", "proposal_expired", False, False),
        ("open", None, False, False),
    ],
)
def test_is_held(status: str, review: str | None, pending: bool, expected: bool) -> None:
    assert is_held(status, review, pending) is expected


def test_expired_proposal_never_goes_past_or_stale() -> None:
    held = is_held("needs_review", "proposal_expired", pending=False)
    assert phase("needs_review", NOW - 10 * DAY, NOW, NOW, held=held) == "overdue"
    assert phase("needs_review", None, NOW - 30 * DAY, NOW, held=held) == "current"


def test_pending_proposal_keeps_an_item_shown() -> None:
    assert phase("open", NOW - 3 * DAY, NOW, NOW, held=True) == "overdue"
    assert phase("needs_review", None, NOW - 15 * DAY, NOW, held=True) == "current"
