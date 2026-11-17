"""Cross-check the parser's due time with the model's suggestion (ADR 0020).

Pure functions only. The model returns `due_utc`, its own reading of
`due_raw` as a UTC time. It is never used as `due_at` here:

- Both agree (the same minute): the parser's result stands, as before.
- Both give a time and they differ: the item needs review
  (`due_disagrees`). It keeps the parser's time, and the model's time is
  offered as a suggestion.
- The parser cannot read it: the item keeps the parser's review reason and
  has no `due_at`. The model's time is offered as a suggestion, unless the
  reason is one of WITHHELD, where the text itself has two readings.
- No suggestion, or one that is not a valid UTC time near the email date:
  the parser's result stands, and nothing is offered.

Only the owner turns a suggestion into a due time (app/db/item_due.py).
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

# The only form accepted: "2026-11-14T07:59Z" or "2026-11-14T07:59:00Z"
UTC_FORM = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z")
# A suggestion must fall near the email date, as ADR 0005's year rule does,
# with a year's room for a date that states its year
EARLIEST = timedelta(days=30)
LATEST = timedelta(days=366)

# Parser reasons where the text itself has two readings, so the model can
# only guess one: its time is kept for the evaluation but not offered
WITHHELD = frozenset(
    {
        "numeric_date",
        "midnight",
        "relative_weekday",
        "ambiguous_time",
        "multiple_dates",
        "multiple_times",
        "multiple_zones",
        "weekday_mismatch",
    }
)
# Reasons where the due time itself is unproven or absent: nothing to check
UNCHECKED = frozenset({"due_not_in_evidence", "no_due_raw"})

DISAGREES = "due_disagrees"

# What the cross-check found, stored as `due_check`
Check = Literal["agree", "disagree", "suggested", "withheld", "none", "invalid"]
# Checks that offer the suggestion to the owner
OFFERED: tuple[Check, ...] = ("disagree", "suggested")


@dataclass(frozen=True)
class Crossed:
    check: Check
    # The model's time, when it is a valid UTC time near the email date
    suggested: datetime | None
    # The review code to use instead of the parser's, or None to keep it
    review: str | None = None

    @property
    def offered(self) -> bool:
        return self.check in OFFERED


NOTHING = Crossed("none", None)


def read_utc(text: str | None, sent_at: datetime) -> datetime | None | Literal["invalid"]:
    """The suggestion as a UTC time; None when absent, "invalid" when unusable."""
    if text is None or not text.strip():
        return None
    value = text.strip()
    if not UTC_FORM.fullmatch(value):
        return "invalid"
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "invalid"
    when = when.astimezone(UTC)
    if when < sent_at - EARLIEST or when > sent_at + LATEST:
        return "invalid"
    return when


def to_minute(when: datetime) -> datetime:
    return when.replace(second=0, microsecond=0)


def same_minute(a: datetime, b: datetime) -> bool:
    """Agreement: the same instant once seconds are dropped.

    The model sees the email's Date header to the minute only, so "within
    48 hours" can differ from the parser in its seconds.
    """
    return to_minute(a) == to_minute(b)


def cross(
    due_at: datetime | None, reason: str | None, due_utc: str | None, sent_at: datetime
) -> Crossed:
    """Compare the parser's result (`due_at` or `reason`) with the model's."""
    if reason in UNCHECKED or (due_at is None and reason is None):
        return NOTHING
    read = read_utc(due_utc, sent_at)
    if read is None:
        return NOTHING
    if isinstance(read, str):
        return Crossed("invalid", None)
    if due_at is not None:
        if same_minute(due_at, read):
            return Crossed("agree", read)
        return Crossed("disagree", read, DISAGREES)
    if reason in WITHHELD:
        return Crossed("withheld", read)
    return Crossed("suggested", read)
