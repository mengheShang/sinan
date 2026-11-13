"""Turn an explicit deadline phrase into a UTC time, or say why it needs review.

Pure and deterministic: no network, no clock, no LLM. The rules (ADR 0005,
extended by ADR 0011):

- A time zone is required. Pacific, Mountain, Central, Eastern, UTC/GMT,
  AoE and China time ("Beijing time", "China Standard Time") are understood.
  "CST" is always US Central. Standard or daylight names (PST, PDT, ...) must
  match the date: "PST" on a summer date is ambiguous.
- A time is required. "noon" is 12:00; "end of day" (or "EOD") is 23:59 in
  the stated zone; "midnight" is ambiguous.
- The date is a month name and day ("Nov 14", "November 14th, 2026"), a day
  and month name ("14 November", "14 Nov 2026"; day-first "May" needs its
  capital, since "2 may be late" is the verb), an ISO date, a weekday
  ("Friday"), or today / tonight / tomorrow. Numeric dates such as 11/14
  always need review, because 11/12 has two readings.
- A missing year is inferred from the email date: the one year that puts the
  date between 30 days before and 335 days after the email.
- A weekday alone means the first such day on or after the email date, in the
  deadline's time zone. "next Friday" needs review.
- A weekday next to a date must agree with it.
- Wall times that do not exist, or happen twice, on a DST change need review,
  unless a standard or daylight name settles it.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

PAST_WINDOW = timedelta(days=30)
FUTURE_WINDOW = timedelta(days=335)


@dataclass(frozen=True)
class Zone:
    region: tzinfo
    # Fixed UTC offset the name implies, or None for "PT" style names
    offset: timedelta | None


def region(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def hours(n: int) -> timedelta:
    return timedelta(hours=n)


ZONES: dict[str, Zone] = {
    "pt": Zone(region("America/Los_Angeles"), None),
    "pacific": Zone(region("America/Los_Angeles"), None),
    "pst": Zone(region("America/Los_Angeles"), hours(-8)),
    "pdt": Zone(region("America/Los_Angeles"), hours(-7)),
    "mt": Zone(region("America/Denver"), None),
    "mountain": Zone(region("America/Denver"), None),
    "mst": Zone(region("America/Denver"), hours(-7)),
    "mdt": Zone(region("America/Denver"), hours(-6)),
    "ct": Zone(region("America/Chicago"), None),
    "central": Zone(region("America/Chicago"), None),
    "cst": Zone(region("America/Chicago"), hours(-6)),
    "cdt": Zone(region("America/Chicago"), hours(-5)),
    "et": Zone(region("America/New_York"), None),
    "eastern": Zone(region("America/New_York"), None),
    "est": Zone(region("America/New_York"), hours(-5)),
    "edt": Zone(region("America/New_York"), hours(-4)),
    "utc": Zone(UTC, hours(0)),
    "gmt": Zone(UTC, hours(0)),
    # Anywhere on Earth: the deadline holds until the day ends everywhere
    "aoe": Zone(timezone(hours(-12)), hours(-12)),
    # China time by full name only; "cst" above stays US Central
    "beijing time": Zone(region("Asia/Shanghai"), None),
    "china standard time": Zone(region("Asia/Shanghai"), hours(8)),
}

# Exact spellings only, so "marks 10" or "month" are not read as dates
MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
WEEKDAYS = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


def words(names: dict[str, int] | dict[str, Zone]) -> str:
    """A regex alternation, longest first so "september" wins over "sep"."""
    ordered = sorted(names, key=len, reverse=True)
    return "|".join(ordered)


ZONE_RE = re.compile(r"\b(" + words(ZONES) + r")\b")
MONTH = r"\b(" + words(MONTHS) + r")\b\.?"
DAY = r"\b(\d{1,2})(?:st|nd|rd|th)?\b"
YEAR = r"(?:\s+(\d{4})\b)?"
# A day after a month is not a clock hour: "14 Nov 11:59pm" and
# "14 Nov 5 pm" do not mean Nov 11 or Nov 5
NOT_HOUR = r"(?![:.]\d)(?!\s*(?:am|pm)\b)"
# "Nov 14", "November 14th 2026"
MONTH_RE = re.compile(MONTH + r"\s+" + DAY + NOT_HOUR + YEAR)
# A day before a month is not right after ":", "-", "." or a digit, so the
# "59" in "23:59 November 14" is not a day
NOT_MINUTE = r"(?<![:\-.\d])"
# "14 Nov", "14th November 2026". Matched on text that keeps its case, so
# "May" can be told from the verb "may" (see day_first_dates)
DAY_FIRST_RE = re.compile(NOT_MINUTE + DAY + r"\s+" + MONTH + YEAR, re.IGNORECASE)
ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
NUMERIC_RE = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
WEEKDAY_RE = re.compile(r"\b(?:(next|this)\s+)?(" + words(WEEKDAYS) + r")\.?(?=\s|$|\b)")
RELATIVE_RE = re.compile(r"\b(today|tonight|tomorrow)\b")
TIME_12_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b")
TIME_24_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b(?!\s*(?:am|pm))")
END_OF_DAY_RE = re.compile(r"\b(?:end[\s-]+of[\s-]+(?:the[\s-]+)?day|eod)\b")
END_OF_DAY = time(23, 59)


@dataclass(frozen=True)
class DueParse:
    """Exactly one field is set: the UTC due time, or why a person must check it."""

    due_at: datetime | None
    reason: str | None


def review(reason: str) -> DueParse:
    return DueParse(None, reason)


def tidy(text: str) -> str:
    """Commas become spaces and spaces collapse; case is kept."""
    spaced = text.replace(",", " ")
    # One space between words, so "beijing  time" matches "beijing time"
    return " ".join(spaced.split())


def normalize(text: str) -> str:
    lowered = tidy(text).lower()
    return lowered.replace("a.m.", "am").replace("p.m.", "pm")


def find_zone(text: str) -> Zone | str:
    names = set(ZONE_RE.findall(text))
    if not names:
        return "no_zone"
    zones = {ZONES[n] for n in names}
    if len(zones) > 1:
        return "multiple_zones"
    return zones.pop()


def find_time(text: str) -> time | str:
    if "midnight" in text:
        return "midnight"
    found: list[time] = []
    if re.search(r"\bnoon\b", text):
        found.append(time(12, 0))
    if END_OF_DAY_RE.search(text):
        found.append(END_OF_DAY)
    for hour_s, minute_s, half in TIME_12_RE.findall(text):
        hour, minute = int(hour_s), int(minute_s or 0)
        if not 1 <= hour <= 12 or minute > 59:
            return "invalid_time"
        hour = hour % 12 + (12 if half == "pm" else 0)
        found.append(time(hour, minute))
    for hour_s, minute_s in TIME_24_RE.findall(text):
        hour, minute = int(hour_s), int(minute_s)
        if hour > 23 or minute > 59:
            return "invalid_time"
        found.append(time(hour, minute))
    if not found:
        return "no_time"
    if len(set(found)) > 1:
        return "multiple_times"
    return found[0]


@dataclass(frozen=True)
class DateParts:
    month: int
    day: int
    year: int | None


def is_verb_may(month: str) -> bool:
    """Lowercase "may" after a number is the verb: "2 may be late"."""
    return month.lower() == "may" and month[0].islower()


def day_first_dates(cased: str) -> set[DateParts]:
    """Day-first dates in text that keeps its case (ADR 0011)."""
    found: set[DateParts] = set()
    for day_s, month_s, year_s in DAY_FIRST_RE.findall(cased):
        if is_verb_may(month_s):
            continue
        year = int(year_s) if year_s else None
        month = MONTHS[month_s.lower()]
        found.add(DateParts(month, int(day_s), year))
    return found


def find_date(text: str, cased: str) -> DateParts | str | None:
    """A written date, a reason it needs review, or None when there is none."""
    if NUMERIC_RE.search(text):
        return "numeric_date"
    found: set[DateParts] = set()
    for month_s, day_s, year_s in MONTH_RE.findall(text):
        year = int(year_s) if year_s else None
        found.add(DateParts(MONTHS[month_s], int(day_s), year))
    found |= day_first_dates(cased)
    for year_s, month_s, day_s in ISO_RE.findall(text):
        found.add(DateParts(int(month_s), int(day_s), int(year_s)))
    if len(found) > 1:
        return "multiple_dates"
    return found.pop() if found else None


def build(parts: DateParts, year: int) -> date | None:
    try:
        return date(year, parts.month, parts.day)
    except ValueError:
        return None


def resolve_year(parts: DateParts, sent: date) -> date | str:
    if parts.year is not None:
        exact = build(parts, parts.year)
        return exact if exact is not None else "invalid_date"
    earliest = sent - PAST_WINDOW
    latest = sent + FUTURE_WINDOW
    options = []
    for year in (sent.year - 1, sent.year, sent.year + 1):
        day = build(parts, year)
        if day is not None and earliest <= day <= latest:
            options.append(day)
    if not options:
        exists = any(build(parts, y) for y in (sent.year, sent.year + 1))
        return "year_unclear" if exists else "invalid_date"
    if len(options) > 1:
        return "year_unclear"
    return options[0]


def find_day(text: str, cased: str, sent: date) -> date | str:
    weekdays = WEEKDAY_RE.findall(text)
    relative = set(RELATIVE_RE.findall(text))
    written = find_date(text, cased)
    if isinstance(written, str):
        return written
    if any(word == "next" for word, _ in weekdays):
        return "relative_weekday"
    named = {WEEKDAYS[w] for _, w in weekdays}
    if len(named) > 1:
        return "multiple_dates"
    if written is not None:
        if relative:
            return "multiple_dates"
        day = resolve_year(written, sent)
        if isinstance(day, str):
            return day
        if named and day.weekday() not in named:
            return "weekday_mismatch"
        return day
    if relative:
        if len(relative) > 1 or named:
            return "multiple_dates"
        offset = 1 if "tomorrow" in relative else 0
        return sent + timedelta(days=offset)
    if named:
        ahead = (named.pop() - sent.weekday()) % 7
        return sent + timedelta(days=ahead)
    return "no_date"


def place(day: date, at: time, zone: Zone) -> datetime | str:
    """Attach the zone to a wall time, refusing times DST makes ambiguous."""
    naive = datetime.combine(day, at)
    first = naive.replace(tzinfo=zone.region, fold=0)
    second = naive.replace(tzinfo=zone.region, fold=1)
    if zone.offset is not None:
        # A named offset settles folds and gaps; it must still fit the date
        valid = {first.utcoffset(), second.utcoffset()}
        if zone.offset not in valid:
            return "zone_dst_mismatch"
        return naive.replace(tzinfo=timezone(zone.offset))
    if first.utcoffset() != second.utcoffset():
        round_trip = first.astimezone(UTC).astimezone(zone.region)
        if round_trip.replace(tzinfo=None) != naive:
            return "nonexistent_time"
        return "ambiguous_time"
    return first


def parse_due(due_raw: str, sent_at: datetime) -> DueParse:
    """Parse `due_raw` against the email's send time (timezone-aware)."""
    if sent_at.tzinfo is None:
        raise ValueError("sent_at must be timezone-aware")
    text = normalize(due_raw)
    cased = tidy(due_raw)
    zone = find_zone(text)
    if isinstance(zone, str):
        return review(zone)
    at = find_time(text)
    if isinstance(at, str):
        return review(at)
    # Relative days count in the deadline's own time zone
    sent_local = sent_at.astimezone(zone.region).date()
    day = find_day(text, cased, sent_local)
    if isinstance(day, str):
        return review(day)
    placed = place(day, at, zone)
    if isinstance(placed, str):
        return review(placed)
    return DueParse(placed.astimezone(UTC), None)
