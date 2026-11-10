"""Table-driven tests for explicit deadline parsing. All times are compared in UTC."""

from datetime import UTC, datetime

import pytest

from app.extraction.deadlines import DueParse, parse_due

# Tuesday Nov 10 2026, 09:00 in Los Angeles (PST, UTC-8)
SENT = datetime(2026, 11, 10, 17, 0, tzinfo=UTC)


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


PARSED = [
    # Month name and day; the year comes from the email date
    ("Nov 14 11:59pm PT", SENT, utc(2026, 11, 15, 7, 59)),
    ("November 14th, 2026 at 11:59 PM PST", SENT, utc(2026, 11, 15, 7, 59)),
    ("due 11:59 p.m. Pacific, November 14", SENT, utc(2026, 11, 15, 7, 59)),
    ("Nov. 14 23:59 PT", SENT, utc(2026, 11, 15, 7, 59)),
    ("Sept 3 5pm ET", datetime(2026, 8, 20, tzinfo=UTC), utc(2026, 9, 3, 21, 0)),
    ("2026-11-14 23:59 UTC", SENT, utc(2026, 11, 14, 23, 59)),
    ("Nov 14 noon CT", SENT, utc(2026, 11, 14, 18, 0)),
    ("Nov 14 11:59pm AoE", SENT, utc(2026, 11, 15, 11, 59)),
    ("Nov 14 5pm MT", SENT, utc(2026, 11, 15, 0, 0)),
    # A weekday next to a matching date
    ("Friday Nov 13 at 11:59pm PT", SENT, utc(2026, 11, 14, 7, 59)),
    # A weekday alone: the first one on or after the email date
    ("Friday 11:59pm PT", SENT, utc(2026, 11, 14, 7, 59)),
    ("this Friday 11:59pm PT", SENT, utc(2026, 11, 14, 7, 59)),
    ("Tuesday 5pm ET", SENT, utc(2026, 11, 10, 22, 0)),
    ("Mon 9am PT", SENT, utc(2026, 11, 16, 17, 0)),
    # Today, tonight, tomorrow, counted in the deadline's zone
    ("tonight 11:59pm PT", SENT, utc(2026, 11, 11, 7, 59)),
    ("tomorrow 9am ET", SENT, utc(2026, 11, 11, 14, 0)),
    # Late on Nov 10 in Los Angeles is already Nov 11 in UTC; "today" stays Nov 10
    ("today 11pm PT", utc(2026, 11, 11, 6, 0), utc(2026, 11, 11, 7, 0)),
    # Year rollover: a December email about January means next year
    ("Jan 5 11:59pm PT", utc(2026, 12, 20, 18, 0), utc(2027, 1, 6, 7, 59)),
    # A January email about late December means the past few days
    ("Dec 28 5pm PT", utc(2027, 1, 3, 18, 0), utc(2026, 12, 29, 1, 0)),
    # Further than 30 days back means next year
    ("Sep 1 5pm PT", SENT, utc(2027, 9, 2, 0, 0)),
    # DST: PT follows the date; PDT before Nov 1 2026, PST after
    ("Oct 20 11:59pm PT", utc(2026, 10, 1), utc(2026, 10, 21, 6, 59)),
    ("Oct 20 11:59pm PDT", utc(2026, 10, 1), utc(2026, 10, 21, 6, 59)),
    # The repeated hour on Nov 1 is settled by a standard or daylight name
    ("Nov 1 1:30am PST", utc(2026, 10, 25), utc(2026, 11, 1, 9, 30)),
    ("Nov 1 1:30am PDT", utc(2026, 10, 25), utc(2026, 11, 1, 8, 30)),
    # Spring forward: 2027-03-14 in the US
    ("Mar 14 3:30am ET", utc(2027, 3, 1), utc(2027, 3, 14, 7, 30)),
]


@pytest.mark.parametrize(("raw", "sent", "expected"), PARSED)
def test_parsed(raw: str, sent: datetime, expected: datetime) -> None:
    assert parse_due(raw, sent) == DueParse(expected, None)


NEEDS_REVIEW = [
    # Missing pieces
    ("Nov 14 11:59pm", SENT, "no_zone"),
    ("Nov 14 PT", SENT, "no_time"),
    ("11:59pm PT", SENT, "no_date"),
    ("", SENT, "no_zone"),
    ("sometime next week PT 5pm", SENT, "no_date"),
    # Numeric dates have two readings
    ("11/14 11:59pm PT", SENT, "numeric_date"),
    ("11/12/2026 5pm PT", SENT, "numeric_date"),
    # Unclear or conflicting
    ("midnight Nov 14 PT", SENT, "midnight"),
    ("next Friday 5pm PT", SENT, "relative_weekday"),
    ("Thursday Nov 13 11:59pm PT", SENT, "weekday_mismatch"),
    ("Nov 14 5pm or Nov 15 5pm PT", SENT, "multiple_dates"),
    ("Nov 14 5pm or 6pm PT", SENT, "multiple_times"),
    ("Nov 14 5pm PT (8pm ET)", SENT, "multiple_zones"),
    ("Friday or Saturday 5pm PT", SENT, "multiple_dates"),
    ("tomorrow Nov 14 5pm PT", SENT, "multiple_dates"),
    # Impossible values
    ("Nov 31 5pm PT", SENT, "invalid_date"),
    ("Nov 14 13pm PT", SENT, "invalid_time"),
    ("Nov 14 25:00 PT", SENT, "invalid_time"),
    ("Feb 29 2027 5pm PT", SENT, "invalid_date"),
    # DST: a standard name on a summer date, a missing hour, a repeated hour
    ("Oct 20 11:59pm PST", utc(2026, 10, 1), "zone_dst_mismatch"),
    ("Dec 1 5pm EDT", SENT, "zone_dst_mismatch"),
    ("Mar 14 2:30am ET", utc(2027, 3, 1), "nonexistent_time"),
    ("Nov 1 1:30am PT", utc(2026, 10, 25), "ambiguous_time"),
]


@pytest.mark.parametrize(("raw", "sent", "reason"), NEEDS_REVIEW)
def test_needs_review(raw: str, sent: datetime, reason: str) -> None:
    assert parse_due(raw, sent) == DueParse(None, reason)


@pytest.mark.parametrize(
    "raw",
    [
        "This assignment marks 10 points, 5pm PT",
        "Due in one month 5pm PT",
        "Submit the second draft at 5pm PT",
    ],
)
def test_words_that_look_like_dates_are_not_dates(raw: str) -> None:
    assert parse_due(raw, SENT).reason == "no_date"


def test_naive_send_time_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_due("Nov 14 5pm PT", datetime(2026, 11, 10, 9, 0))


def test_result_is_always_one_or_the_other() -> None:
    for raw, sent, _ in PARSED + NEEDS_REVIEW:
        result = parse_due(raw, sent)
        assert (result.due_at is None) != (result.reason is None)
        if result.due_at is not None:
            assert result.due_at.tzinfo == UTC
