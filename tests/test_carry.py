"""Keeping the earlier time of day on a date-only change (ADR 0016, decision 7).

Pure tests of app/items/carry.py. All times are compared in UTC.
"""

from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.extraction.validate import ValidItem
from app.items.assumed import NOTHING, Assumed
from app.items.carry import carry_time

LA = ZoneInfo("America/Los_Angeles")


def utc(month: int, day: int, hour: int, minute: int = 0, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def change(due_raw: str, due_at: datetime, zone: bool = True, time: bool = True) -> ValidItem:
    """A parsed change, as validation returns it."""
    return ValidItem(
        kind="deadline",
        course="CS 000",
        title="Homework 3",
        due_raw=due_raw,
        due_at=due_at,
        status="needs_review",
        review="unmatched_update",
        evidence=f"Homework 3 is now due {due_raw}",
        change_type="update",
        zone_assumed=zone,
        time_assumed=time,
    )


# "Nov 20" alone: 23:59 PST, zone and time assumed
NOV_20 = change("Nov 20", utc(11, 21, 7, 59))
# The item: Friday Nov 13 at 5pm PT, written in full
FRIDAY_5PM = ("Friday Nov 13 at 5pm PT", utc(11, 14, 1, 0))


def test_date_only_change_keeps_5pm() -> None:
    result = carry_time(NOV_20, *FRIDAY_5PM, NOTHING, LA)
    assert result.due_at == utc(11, 21, 1, 0)
    assert result.time_carried
    assert not result.time_assumed
    # The item stated PT, so the zone is no longer assumed
    assert not result.zone_assumed


def test_wall_clock_time_survives_dst() -> None:
    # 5pm PDT on Oct 30 is 00:00 UTC; on Nov 6 (PST) 5pm is 01:00 UTC
    item = ("Oct 30 at 5pm PT", utc(10, 31, 0, 0))
    nov_6 = change("Nov 6", utc(11, 7, 7, 59))
    result = carry_time(nov_6, *item, NOTHING, LA)
    assert result.due_at == utc(11, 7, 1, 0)
    assert result.time_carried


def test_spring_forward_keeps_5pm() -> None:
    item = ("Mar 5 2027 at 5pm PT", utc(3, 6, 1, 0, 2027))
    mar_20 = change("Mar 20 2027", utc(3, 21, 6, 59, 2027))
    result = carry_time(mar_20, *item, NOTHING, LA)
    assert result.due_at == utc(3, 21, 0, 0, 2027)


def test_earlier_assumed_time_stays_23_59() -> None:
    item = ("Friday Nov 13", utc(11, 14, 7, 59))
    flags = Assumed(zone=True, time=True)
    assert carry_time(NOV_20, *item, flags, LA) == NOV_20


def test_a_change_with_its_own_time_uses_it() -> None:
    own = change("Nov 20 at 9am", utc(11, 20, 17, 0), time=False)
    assert carry_time(own, *FRIDAY_5PM, NOTHING, LA) == own


def test_zone_stays_assumed_when_neither_email_states_one() -> None:
    item = ("Friday Nov 13 at 5pm", utc(11, 14, 1, 0))
    result = carry_time(NOV_20, *item, Assumed(zone=True), LA)
    assert result.due_at == utc(11, 21, 1, 0)
    assert result.zone_assumed


def test_item_zone_is_kept() -> None:
    # The item said ET; the change's date is placed at 5pm ET
    item = ("Friday Nov 13 at 5pm ET", utc(11, 13, 22, 0))
    result = carry_time(NOV_20, *item, NOTHING, LA)
    assert result.due_at == utc(11, 20, 22, 0)
    assert not result.zone_assumed


def test_same_stated_zone_carries() -> None:
    stated = change("Nov 20 PT", utc(11, 21, 7, 59), zone=False)
    result = carry_time(stated, *FRIDAY_5PM, NOTHING, LA)
    assert result.due_at == utc(11, 21, 1, 0)


@pytest.mark.parametrize(
    ("item", "flags"),
    [
        # The change states ET, the item PT: no single zone for the time
        (FRIDAY_5PM, NOTHING),
        # The item has no due time
        (("Friday Nov 13 at 5pm PT", None), NOTHING),
        # The item's zone needs review, so its wall time is unknown
        (("Friday Nov 13 at 5pm JST", utc(11, 13, 8, 0)), NOTHING),
    ],
)
def test_nothing_is_carried(item: tuple[str, datetime | None], flags: Assumed) -> None:
    stated = change("Nov 20 ET", utc(11, 21, 4, 59), zone=False)
    assert carry_time(stated, *item, flags, LA) == stated


def test_time_missing_on_the_new_date_is_not_carried() -> None:
    # 2:30am does not exist on Mar 14 2027 in Los Angeles
    item = ("Mar 7 2027 at 2:30am PT", utc(3, 7, 10, 30, 2027))
    mar_14 = change("Mar 14 2027", utc(3, 15, 6, 59, 2027))
    assert carry_time(mar_14, *item, NOTHING, LA) == mar_14


def test_a_change_without_a_due_time_is_left_alone() -> None:
    nothing = replace(NOV_20, due_at=None, time_assumed=False, zone_assumed=False)
    assert carry_time(nothing, *FRIDAY_5PM, NOTHING, LA) == nothing
