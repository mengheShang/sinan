"""Item phases on a fixed clock (ADR 0015), without a database."""

from datetime import UTC, datetime, timedelta

import pytest

from app.items.timing import Phase, phase

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
    assert phase("open", due, NOW, NOW, pending=False) == expected


def test_due_time_applies_to_review_items_too() -> None:
    assert phase("needs_review", NOW - 3 * DAY, NOW, NOW, pending=False) == "past"


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (13 * DAY, "current"),
        (14 * DAY, "current"),
        (15 * DAY, "stale"),
    ],
)
def test_review_item_without_due_time_goes_stale(age: timedelta, expected: Phase) -> None:
    assert phase("needs_review", None, NOW - age, NOW, pending=False) == expected


def test_open_item_without_due_time_never_goes_stale() -> None:
    assert phase("open", None, NOW - 60 * DAY, NOW, pending=False) == "current"


def test_pending_proposal_keeps_an_item_shown() -> None:
    assert phase("open", NOW - 3 * DAY, NOW, NOW, pending=True) == "overdue"
    assert phase("needs_review", None, NOW - 15 * DAY, NOW, pending=True) == "current"
