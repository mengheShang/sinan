"""Where an active item stands in time, computed on read (ADR 0015).

Pure functions: the caller passes the clock. Nothing here changes an item's
status; past and stale items only move out of the digest into the Past view.

- current: due in the future, or no due time and not stale.
- overdue: due time passed less than GRACE ago. Shown in the digest's
  "Overdue" group.
- past: due time passed GRACE or more ago. Shown only in the Past view.
- stale: needs review, no due time, and the source email is more than
  STALE_AFTER old. Shown only in the Past view.

An item is held in the digest while a proposal is waiting on it (ADR 0014):
it is never past or stale, and once its due time passes it stays overdue.
That covers a valid pending proposal, and an item in review because its
proposal expired unanswered. Its stored due time is the old one, while the
proposed later time may still be ahead, so it must not drop out unseen.
"""

from datetime import datetime, timedelta
from typing import Literal

GRACE = timedelta(days=2)
STALE_AFTER = timedelta(days=14)

Phase = Literal["current", "overdue", "past", "stale"]
# Phases that keep an item on the Dashboard
SHOWN: tuple[Phase, ...] = ("current", "overdue")
# Review codes that hold an item in the digest like a pending proposal. A
# later email moved the due time but it did not parse (ADR 0023): the stored
# one is old, so the item must not drop out unseen either.
HOLDING_REVIEWS = frozenset({"proposal_expired", "update_unparsed"})


def is_held(status: str, review: str | None, pending: bool) -> bool:
    """Whether a proposal keeps the item in the digest whatever its due time."""
    if pending:
        return True
    return status == "needs_review" and review in HOLDING_REVIEWS


def due_phase(due_at: datetime, now: datetime, held: bool) -> Phase:
    if due_at >= now:
        return "current"
    if held or now - due_at < GRACE:
        return "overdue"
    return "past"


def is_stale(status: str, source_at: datetime, now: datetime) -> bool:
    return status == "needs_review" and now - source_at > STALE_AFTER


def phase(
    status: str,
    due_at: datetime | None,
    source_at: datetime,
    now: datetime,
    held: bool,
) -> Phase:
    """The phase of an open or needs_review item at `now`.

    `source_at` is when the item's source email arrived, or when the item
    was created if no email is known. `held` says whether a proposal keeps the
    item in the digest (see is_held).
    """
    if due_at is not None:
        return due_phase(due_at, now, held)
    if not held and is_stale(status, source_at, now):
        return "stale"
    return "current"
