"""Where an active item stands in time, computed on read (ADR 0015).

Pure functions: the caller passes the clock. Nothing here changes an item's
status; past and stale items only move out of the digest into the Past view.

- current: due in the future, or no due time and not stale.
- overdue: due time passed less than GRACE ago. Shown in the digest's
  "Overdue" group.
- past: due time passed GRACE or more ago. Shown only in the Past view.
- stale: needs review, no due time, and the source email is more than
  STALE_AFTER old. Shown only in the Past view.

An item with a valid pending proposal stays in the digest (ADR 0014): it is
never past or stale, and once its due time passes it stays overdue.
"""

from datetime import datetime, timedelta
from typing import Literal

GRACE = timedelta(days=2)
STALE_AFTER = timedelta(days=14)

Phase = Literal["current", "overdue", "past", "stale"]
# Phases that keep an item on the Dashboard
SHOWN: tuple[Phase, ...] = ("current", "overdue")


def due_phase(due_at: datetime, now: datetime, pending: bool) -> Phase:
    if due_at >= now:
        return "current"
    if pending or now - due_at < GRACE:
        return "overdue"
    return "past"


def is_stale(status: str, source_at: datetime, now: datetime) -> bool:
    return status == "needs_review" and now - source_at > STALE_AFTER


def phase(
    status: str,
    due_at: datetime | None,
    source_at: datetime,
    now: datetime,
    pending: bool,
) -> Phase:
    """The phase of an open or needs_review item at `now`.

    `source_at` is when the item's source email arrived, or when the item
    was created if no email is known. `pending` says whether the item has a
    proposal that can still be decided.
    """
    if due_at is not None:
        return due_phase(due_at, now, pending)
    if not pending and is_stale(status, source_at, now):
        return "stale"
    return "current"
