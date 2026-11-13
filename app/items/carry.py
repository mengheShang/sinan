"""Keep the earlier time of day when a deadline change gives only a date.

ADR 0016, decision 7 (owner, 2026-11-13). Pure functions only.

"Homework 3 is now due Nov 20" parses as Nov 20, 23:59, time not stated.
When the matched item's current due time had a written time of day, such as
5:00 PM PT, the proposal keeps it: Nov 20, 5:00 PM in the item's zone. The
time is the item's wall-clock time in its zone, not its UTC offset, so a
change across a DST boundary still says 5:00 PM PT.

Nothing is carried, and the change keeps 23:59 "time not stated", when:

- the change gave its own time, or no time was assumed for it;
- the item has no due time, or its time was itself assumed (23:59);
- either zone cannot be read from its due_raw, or the change states a zone
  that differs from the item's;
- the time does not exist, or happens twice, on the new date (DST).
"""

from dataclasses import replace
from datetime import UTC, datetime, tzinfo

from app.extraction.deadlines import Zone, place, zone_of
from app.extraction.validate import ValidItem
from app.items.assumed import Assumed


def carry_time(
    change: ValidItem,
    due_raw: str | None,
    due_at: datetime | None,
    flags: Assumed,
    home: tzinfo,
) -> ValidItem:
    """The change with the earlier item's time of day, or unchanged.

    `due_raw`, `due_at` and `flags` describe the item's current due time;
    `home` is SINAN_TIMEZONE.
    """
    if not change.time_assumed or change.due_at is None or change.due_raw is None:
        return change
    if due_at is None or due_raw is None or flags.time:
        return change
    change_zone = zone_of(change.due_raw, home)
    item_zone = zone_of(due_raw, home)
    if change_zone is None or item_zone is None:
        return change
    if not change.zone_assumed and change_zone != item_zone:
        return change
    day = change.due_at.astimezone(change_zone).date()
    wall = due_at.astimezone(item_zone).time()
    placed = place(day, wall, Zone(item_zone, None))
    if isinstance(placed, str):
        return change
    return replace(
        change,
        due_at=placed.astimezone(UTC),
        # The zone is assumed only if neither email stated one
        zone_assumed=change.zone_assumed and flags.zone,
        time_assumed=False,
        time_carried=True,
    )
