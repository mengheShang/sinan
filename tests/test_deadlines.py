"""Table-driven tests for explicit deadline parsing. All times are compared in UTC."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

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
    # Missing pieces. A missing zone or time is assumed since ADR 0016; see
    # CHANGED_BY_ADR_0016 for the cases that used to be here
    ("11:59pm PT", SENT, "no_date"),
    ("", SENT, "no_date"),
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


# ADR 0011: day-first month names, "end of day" and China time
PARSED_ADR_0011 = [
    # Day-first month names, with and without a year
    ("14 November 5pm PT", SENT, utc(2026, 11, 15, 1, 0)),
    ("14 Nov 2026 11:59pm PT", SENT, utc(2026, 11, 15, 7, 59)),
    ("14th Nov. 11:59pm PT", SENT, utc(2026, 11, 15, 7, 59)),
    ("14 Nov 5 pm PT", SENT, utc(2026, 11, 15, 1, 0)),
    ("by 5pm PT on Saturday 14 November", SENT, utc(2026, 11, 15, 1, 0)),
    ("due 23:59 November 14 PT", SENT, utc(2026, 11, 15, 7, 59)),
    ("5 Jan 11:59pm PT", utc(2026, 12, 20, 18, 0), utc(2027, 1, 6, 7, 59)),
    # Only "May" needs its capital; other day-first months ignore case
    ("14 May 2027 5pm PT", SENT, utc(2027, 5, 15, 0, 0)),
    ("14 MAY 2027 5pm PT", SENT, utc(2027, 5, 15, 0, 0)),
    ("14 nov 5pm PT", SENT, utc(2026, 11, 15, 1, 0)),
    # Lowercase "may" after a number is the verb, not a date
    ("2 may be late, Nov 14 5pm PT", SENT, utc(2026, 11, 15, 1, 0)),
    # Month-first "may" keeps ADR 0005 behavior and ignores case
    ("may 14 5pm PT", SENT, utc(2027, 5, 15, 0, 0)),
    # "end of day" is 23:59 in the stated zone
    ("Nov 14 end of day PT", SENT, utc(2026, 11, 15, 7, 59)),
    ("Nov 14 by end of the day ET", SENT, utc(2026, 11, 15, 4, 59)),
    ("14 Nov EOD PT", SENT, utc(2026, 11, 15, 7, 59)),
    ("Nov 14 end-of-day UTC", SENT, utc(2026, 11, 14, 23, 59)),
    ("Nov 14 EOD AoE", SENT, utc(2026, 11, 15, 11, 59)),
    ("Nov 14 11:59pm EOD PT", SENT, utc(2026, 11, 15, 7, 59)),
    ("tomorrow EOD ET", SENT, utc(2026, 11, 12, 4, 59)),
    # China time is UTC+8 all year
    ("Nov 14 5pm Beijing time", SENT, utc(2026, 11, 14, 9, 0)),
    ("2026-11-14 9am China Standard Time", SENT, utc(2026, 11, 14, 1, 0)),
    ("14 July 5pm Beijing time", SENT, utc(2027, 7, 14, 9, 0)),
    ("14 July 5pm China Standard Time", SENT, utc(2027, 7, 14, 9, 0)),
    ("Nov 14 EOD Beijing time", SENT, utc(2026, 11, 14, 15, 59)),
    # 01:00 Nov 11 in Beijing when sent, so "tomorrow" is Nov 12 there
    ("tomorrow EOD Beijing time", SENT, utc(2026, 11, 12, 15, 59)),
    # "CST" keeps meaning US Central
    ("Nov 14 5pm CST", SENT, utc(2026, 11, 14, 23, 0)),
    # DST edges: end of day on the fall-back and spring-forward days
    ("Nov 1 EOD PT", utc(2026, 10, 25), utc(2026, 11, 2, 7, 59)),
    ("Nov 1 EOD PST", utc(2026, 10, 25), utc(2026, 11, 2, 7, 59)),
    ("Mar 14 EOD ET", utc(2027, 3, 1), utc(2027, 3, 15, 3, 59)),
    ("Oct 31 EOD PT", utc(2026, 10, 25), utc(2026, 11, 1, 6, 59)),
    # DST edges with day-first dates: the repeated hour settled by a name
    ("1 Nov 1:30am PST", utc(2026, 10, 25), utc(2026, 11, 1, 9, 30)),
    ("1 Nov 1:30am PDT", utc(2026, 10, 25), utc(2026, 11, 1, 8, 30)),
    ("14 Mar 2027 3:30am ET", utc(2027, 3, 1), utc(2027, 3, 14, 7, 30)),
]


@pytest.mark.parametrize(("raw", "sent", "expected"), PARSED_ADR_0011)
def test_parsed_adr_0011(raw: str, sent: datetime, expected: datetime) -> None:
    assert parse_due(raw, sent) == DueParse(expected, None)


NEEDS_REVIEW_ADR_0011 = [
    # End of day next to a different time
    ("Nov 14 EOD 5pm PT", SENT, "multiple_times"),
    # Other "end of" phrases are not end of day, and 23:59 is not assumed
    ("Nov 14 end of week PT", SENT, "vague_time"),
    # Midnight, numeric dates and conflicts still need review
    ("midnight 14 Nov PT", SENT, "midnight"),
    ("14/11 5pm PT", SENT, "numeric_date"),
    ("Thursday 14 November 5pm PT", SENT, "weekday_mismatch"),
    ("14 Nov or 15 Nov 5pm PT", SENT, "multiple_dates"),
    ("Nov 14 or 15 Nov 5pm PT", SENT, "multiple_dates"),
    ("31 Nov 5pm PT", SENT, "invalid_date"),
    ("29 Feb 2027 5pm PT", SENT, "invalid_date"),
    # Day-first lowercase "may" is the verb, so there is no date
    ("14 may 5pm PT", SENT, "no_date"),
    ("14 may 2027 5pm PT", SENT, "no_date"),
    # China time only by full name; "CST" next to it is US Central
    ("Nov 14 5pm Beijing time (CST)", SENT, "multiple_zones"),
    # A named zone that is not understood is never replaced by the owner's
    ("Nov 14 5pm China time", SENT, "unknown_zone"),
    ("Nov 14 5pm Beijing", SENT, "unknown_zone"),
    # A standard or daylight name must fit the date at 23:59
    ("Nov 1 EOD PDT", utc(2026, 10, 25), "zone_dst_mismatch"),
    ("Mar 14 EOD EST", utc(2027, 3, 1), "zone_dst_mismatch"),
    # DST gaps and overlaps with day-first dates
    ("14 Mar 2:30am ET", utc(2027, 3, 1), "nonexistent_time"),
    ("1 Nov 1:30am PT", utc(2026, 10, 25), "ambiguous_time"),
]


@pytest.mark.parametrize(("raw", "sent", "reason"), NEEDS_REVIEW_ADR_0011)
def test_needs_review_adr_0011(raw: str, sent: datetime, reason: str) -> None:
    assert parse_due(raw, sent) == DueParse(None, reason)


@pytest.mark.parametrize(
    "raw",
    [
        # Clock minutes, ranges and decimals before a month are not days
        "23:59 November 14 PT",
        "Questions 1-3 may be submitted Nov 14 11:59pm PT",
        "11.30 November 14 11:59pm PT",
    ],
)
def test_numbers_before_a_month_are_not_days(raw: str) -> None:
    assert parse_due(raw, SENT) == DueParse(utc(2026, 11, 15, 7, 59), None)


# ADR 0011 decision 7: strict GMT/UTC offsets with the literal sign
PARSED_OFFSETS = [
    # Positive and negative whole hours
    ("Nov 14 5pm GMT+8", SENT, utc(2026, 11, 14, 9, 0)),
    ("Nov 14 5pm UTC-5", SENT, utc(2026, 11, 14, 22, 0)),
    # Minutes, with and without a leading zero on the hour
    ("Nov 14 5pm UTC+05:30", SENT, utc(2026, 11, 14, 11, 30)),
    ("Nov 14 5pm UTC+8:00", SENT, utc(2026, 11, 14, 9, 0)),
    ("Nov 14 5pm UTC+5:45", SENT, utc(2026, 11, 14, 11, 15)),
    ("Nov 14 5pm UTC-03:30", SENT, utc(2026, 11, 14, 20, 30)),
    # The four-digit form
    ("Nov 14 5pm GMT+0800", SENT, utc(2026, 11, 14, 9, 0)),
    ("Nov 14 5pm UTC-0500", SENT, utc(2026, 11, 14, 22, 0)),
    # The ends of the range
    ("Nov 14 5pm UTC+14", SENT, utc(2026, 11, 14, 3, 0)),
    ("Nov 14 5pm UTC-12", SENT, utc(2026, 11, 15, 5, 0)),
    ("Nov 14 5pm UTC+00", SENT, utc(2026, 11, 14, 17, 0)),
    # Offsets work with end of day, relative days and day-first dates
    ("Nov 14 EOD UTC+8", SENT, utc(2026, 11, 14, 15, 59)),
    ("tomorrow 9am UTC+8", SENT, utc(2026, 11, 12, 1, 0)),
    ("GMT+8 14 May 2027 5pm", SENT, utc(2027, 5, 14, 9, 0)),
    # The same offset written twice is one zone
    ("Nov 14 5pm GMT+8 (UTC+08:00)", SENT, utc(2026, 11, 14, 9, 0)),
    # No DST: the US fall-back night does not matter to a fixed offset
    ("Nov 1 1:30am UTC-8", utc(2026, 10, 25), utc(2026, 11, 1, 9, 30)),
]


@pytest.mark.parametrize(("raw", "sent", "expected"), PARSED_OFFSETS)
def test_parsed_offsets(raw: str, sent: datetime, expected: datetime) -> None:
    assert parse_due(raw, sent) == DueParse(expected, None)


def test_offset_sign_is_literal() -> None:
    # GMT+8 is UTC+08:00, not Etc/GMT+8 (UTC-8), which would give 01:00Z Nov 15
    result = parse_due("Nov 14 5pm GMT+8", SENT)
    assert result.due_at == utc(2026, 11, 14, 9, 0)


NEEDS_REVIEW_OFFSETS = [
    # A space between the zone and the sign
    ("Nov 14 5pm GMT +8", SENT, "bad_offset"),
    ("Nov 14 5pm UTC - 5", SENT, "bad_offset"),
    # Hours out of range
    ("Nov 14 5pm UTC+15", SENT, "bad_offset"),
    ("Nov 14 5pm UTC-13", SENT, "bad_offset"),
    ("Nov 14 5pm UTC+14:30", SENT, "bad_offset"),
    # Odd minutes or digit counts
    ("Nov 14 5pm GMT+8:20", SENT, "bad_offset"),
    ("Nov 14 5pm GMT+800", SENT, "bad_offset"),
    # An offset after a regional zone
    ("Nov 14 5pm PT+1", SENT, "bad_offset"),
    ("Nov 14 5pm EST-5", SENT, "bad_offset"),
    # An offset and a different zone conflict
    ("Nov 14 5pm GMT+8 PT", SENT, "multiple_zones"),
    ("Nov 14 5pm GMT+8 or GMT+9", SENT, "multiple_zones"),
]


@pytest.mark.parametrize(("raw", "sent", "reason"), NEEDS_REVIEW_OFFSETS)
def test_needs_review_offsets(raw: str, sent: datetime, reason: str) -> None:
    assert parse_due(raw, sent) == DueParse(None, reason)


# ADR 0016: an assumed zone (the owner's) and an assumed time (23:59)
ZONE, TIME, BOTH, NONE = (True, False), (False, True), (True, True), (False, False)
# Wednesday Nov 11 and Friday Nov 13 2026, 09:00 in Los Angeles
WEDNESDAY = utc(2026, 11, 11, 17, 0)
FRIDAY = utc(2026, 11, 13, 17, 0)
# Friday Nov 13, 21:00 in Los Angeles, already Saturday in UTC
FRIDAY_NIGHT = utc(2026, 11, 14, 5, 0)

PARSED_ADR_0016 = [
    # Rule 1: no zone means the owner's zone, in winter (PST) and summer (PDT)
    ("Nov 14 11:59pm", SENT, utc(2026, 11, 15, 7, 59), ZONE),
    ("Nov 14 5pm", SENT, utc(2026, 11, 15, 1, 0), ZONE),
    ("Jul 14 5pm", utc(2027, 6, 1), utc(2027, 7, 15, 0, 0), ZONE),
    ("2027-07-14 9am", utc(2027, 6, 1), utc(2027, 7, 14, 16, 0), ZONE),
    ("Nov 14 EOD", SENT, utc(2026, 11, 15, 7, 59), ZONE),
    ("end of day Friday", SENT, utc(2026, 11, 14, 7, 59), ZONE),
    ("tomorrow 9am", SENT, utc(2026, 11, 11, 17, 0), ZONE),
    # DST edges in the assumed zone: the day of the change, either side of it
    ("Nov 1 EOD", utc(2026, 10, 25), utc(2026, 11, 2, 7, 59), ZONE),
    ("Oct 31 11:59pm", utc(2026, 10, 25), utc(2026, 11, 1, 6, 59), ZONE),
    ("Nov 1 3am", utc(2026, 10, 25), utc(2026, 11, 1, 11, 0), ZONE),
    ("Mar 14 2027 3:30am", utc(2027, 3, 1), utc(2027, 3, 14, 10, 30), ZONE),
    ("Mar 13 2027 11:59pm", utc(2027, 3, 1), utc(2027, 3, 14, 7, 59), ZONE),
    # Rule 2: no time means 23:59 in the stated or assumed zone
    ("Nov 14 PT", SENT, utc(2026, 11, 15, 7, 59), TIME),
    ("Nov 14 ET", SENT, utc(2026, 11, 15, 4, 59), TIME),
    ("14 Nov Beijing time", SENT, utc(2026, 11, 14, 15, 59), TIME),
    ("Nov 14", SENT, utc(2026, 11, 15, 7, 59), BOTH),
    ("Friday Nov 13", SENT, utc(2026, 11, 14, 7, 59), BOTH),
    ("by Friday", SENT, utc(2026, 11, 14, 7, 59), BOTH),
    ("tomorrow", SENT, utc(2026, 11, 12, 7, 59), BOTH),
    ("Jul 14", utc(2027, 6, 1), utc(2027, 7, 15, 6, 59), BOTH),
    ("Mar 14 2027", utc(2027, 3, 1), utc(2027, 3, 15, 6, 59), BOTH),
    # Rule 3: the first such weekday strictly after the email's local date
    ("the first Friday after this email", WEDNESDAY, utc(2026, 11, 14, 7, 59), BOTH),
    ("the first Friday after this email", FRIDAY, utc(2026, 11, 21, 7, 59), BOTH),
    ("first Friday after this email", FRIDAY_NIGHT, utc(2026, 11, 21, 7, 59), BOTH),
    ("first Friday after today at 5pm PT", FRIDAY, utc(2026, 11, 21, 1, 0), NONE),
    ("the first Fri. after this e-mail 5pm", WEDNESDAY, utc(2026, 11, 14, 1, 0), ZONE),
    ("the Friday following this message, EOD ET", FRIDAY, utc(2026, 11, 21, 4, 59), NONE),
    ("first Wednesday after this announcement", WEDNESDAY, utc(2026, 11, 19, 7, 59), BOTH),
    ("Friday, the first Friday after this email", WEDNESDAY, utc(2026, 11, 14, 7, 59), BOTH),
    # A weekday alone still counts from the email date itself
    ("Friday", FRIDAY, utc(2026, 11, 14, 7, 59), BOTH),
    ("Friday", FRIDAY_NIGHT, utc(2026, 11, 14, 7, 59), BOTH),
    # A written zone and time are never marked as assumed
    ("Nov 14 5pm PT", SENT, utc(2026, 11, 15, 1, 0), NONE),
]


@pytest.mark.parametrize(("raw", "sent", "expected", "assumed"), PARSED_ADR_0016)
def test_parsed_adr_0016(
    raw: str, sent: datetime, expected: datetime, assumed: tuple[bool, bool]
) -> None:
    zone_assumed, time_assumed = assumed
    assert parse_due(raw, sent) == DueParse(expected, None, zone_assumed, time_assumed)


def test_assumed_zone_is_the_owners() -> None:
    eastern = ZoneInfo("America/New_York")
    result = parse_due("Nov 14 5pm", SENT, eastern)
    assert result == DueParse(utc(2026, 11, 14, 22, 0), None, True, False)
    # "today" counts in the assumed zone: 23:30 Nov 10 in LA is Nov 11 in New York
    late = utc(2026, 11, 11, 7, 30)
    assert parse_due("today", late, eastern).due_at == utc(2026, 11, 12, 4, 59)
    assert parse_due("today", late).due_at == utc(2026, 11, 11, 7, 59)


NEEDS_REVIEW_ADR_0016 = [
    # DST gaps and overlaps still need review in the assumed zone
    ("Nov 1 1:30am", utc(2026, 10, 25), "ambiguous_time"),
    ("Mar 14 2027 2:30am", utc(2027, 3, 1), "nonexistent_time"),
    # Rule 4: numeric dates, midnight, "next", conflicts
    ("11/14", SENT, "numeric_date"),
    ("11/14 5pm", SENT, "numeric_date"),
    ("midnight Nov 14", SENT, "midnight"),
    ("Nov 14 at midnight", SENT, "midnight"),
    ("next Friday", SENT, "relative_weekday"),
    ("next Friday 5pm", SENT, "relative_weekday"),
    ("Thursday Nov 13", SENT, "weekday_mismatch"),
    ("Friday or Saturday", SENT, "multiple_dates"),
    ("first Friday after this email, Nov 20", WEDNESDAY, "multiple_dates"),
    ("first Friday after this email or Monday", WEDNESDAY, "multiple_dates"),
    # Rule 4: days counted from another event
    ("the Friday after the midterm", SENT, "relative_event"),
    ("the Friday after the midterm 5pm PT", SENT, "relative_event"),
    ("the first Friday after the midterm", SENT, "relative_event"),
    ("the Friday before the exam 5pm PT", SENT, "relative_event"),
    # Without a time, "before" already stops 23:59 from being assumed
    ("the Friday before the exam", SENT, "vague_time"),
    ("the day after Nov 14", SENT, "relative_event"),
    ("Friday following spring break", SENT, "relative_event"),
    # Rule 4: spans
    ("in two weeks", SENT, "no_date"),
    ("in 2 weeks 5pm PT", SENT, "no_date"),
    ("Friday in two weeks", SENT, "relative_span"),
    ("a week from Friday", SENT, "relative_span"),
    ("Friday next week", SENT, "relative_span"),
    # A missing time tied to something else is not 23:59
    ("Nov 14 before class", SENT, "vague_time"),
    ("Nov 14 in lecture", SENT, "vague_time"),
    ("Friday morning", SENT, "vague_time"),
    ("Friday afternoon PT", SENT, "vague_time"),
    ("Nov 14 close of business", SENT, "vague_time"),
    ("before Friday", SENT, "vague_time"),
    # A named zone that is not understood is never replaced by the owner's
    ("Nov 14 5pm JST", SENT, "unknown_zone"),
    ("Nov 14 5pm BST", SENT, "unknown_zone"),
    ("Nov 14 5pm local time", SENT, "unknown_zone"),
    ("Nov 14 5pm your time", SENT, "unknown_zone"),
    ("Nov 14 5pm London time", SENT, "unknown_zone"),
    ("Nov 14 5pm Hong Kong time", SENT, "unknown_zone"),
    ("Nov 14 5pm PT (London time)", SENT, "multiple_zones"),
    ("Nov 14 5pm Central European Time", SENT, "multiple_zones"),
]


@pytest.mark.parametrize(("raw", "sent", "reason"), NEEDS_REVIEW_ADR_0016)
def test_needs_review_adr_0016(raw: str, sent: datetime, reason: str) -> None:
    assert parse_due(raw, sent) == DueParse(None, reason)


# Cases whose expected result ADR 0016 changed; the comment gives the old one
CHANGED_BY_ADR_0016 = [
    # was no_zone: the owner's zone is assumed
    ("Nov 14 11:59pm", DueParse(utc(2026, 11, 15, 7, 59), None, True, False)),
    ("Nov 14 EOD", DueParse(utc(2026, 11, 15, 7, 59), None, True, False)),
    ("end of day Friday", DueParse(utc(2026, 11, 14, 7, 59), None, True, False)),
    # was no_time: 23:59 is assumed
    ("Nov 14 PT", DueParse(utc(2026, 11, 15, 7, 59), None, False, True)),
    # was no_zone: a missing zone no longer stops parsing, so the missing date shows
    ("", DueParse(None, "no_date")),
    # was no_time: "end of week" is not a time, and 23:59 is not assumed for it
    ("Nov 14 end of week PT", DueParse(None, "vague_time")),
    # was no_zone: a zone is named but not understood
    ("Nov 14 5pm China time", DueParse(None, "unknown_zone")),
    ("Nov 14 5pm Beijing", DueParse(None, "unknown_zone")),
]


@pytest.mark.parametrize(("raw", "expected"), CHANGED_BY_ADR_0016)
def test_changed_by_adr_0016(raw: str, expected: DueParse) -> None:
    assert parse_due(raw, SENT) == expected


def test_flags_only_come_with_a_due_time() -> None:
    cases = [(raw, sent) for raw, sent, *_ in PARSED_ADR_0016 + NEEDS_REVIEW_ADR_0016]
    for raw, sent in cases:
        result = parse_due(raw, sent)
        if result.due_at is None:
            assert not result.zone_assumed
            assert not result.time_assumed
