"""The owner sets a review item's due time from the Dashboard (ADR 0020).

This is how a suggestion from the model becomes a due time: the owner
confirms it in one click, keeps the parser's time, or types another. The
model's time is never applied any other way.

Allowed only on a deadline or to-do in `needs_review` whose reason is about
its due time or its sender. An item waiting on a change link or an expired
proposal keeps its other path. The item becomes `open`, except one held as
`untrusted_sender` (ADR 0010): a confirmed time gives its sender no trust,
so it stays held.

The change appends an `item_versions` row with actor `user` and updates
`items.due_at` and `items.status` in the same transaction, plus an audit
event with IDs and codes only. Local database writes only: nothing reaches
Gmail or any other service, and the model plays no part.
"""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, tzinfo
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.item_status import UnknownItem, next_version
from app.db.models import AuditEvent, Item, ItemVersion
from app.extraction.crosscheck import OFFERED, same_minute
from app.extraction.validate import UNTRUSTED
from app.items.assumed import NOTHING, assumed_fields

# Review reasons that are not about the due time; they have their own path
OTHER_REVIEWS = frozenset({"unmatched_update", "ambiguous_update", "proposal_expired"})

# Where the new time came from, stored as `due_source`
Source = Literal["suggestion", "parser", "owner"]


class NotReviewable(Exception):
    """The item is not a deadline or to-do waiting on its due time."""


class BadLocalTime(Exception):
    """A wall time that does not exist, or happens twice, in the owner's zone."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def can_set_due(kind: str, status: str, review: str | None) -> bool:
    return kind != "notice" and status == "needs_review" and review not in OTHER_REVIEWS


def from_local(wall: datetime, zone: tzinfo) -> datetime:
    """A naive wall time in `zone` as UTC. Refuses DST gaps and overlaps."""
    early = wall.replace(tzinfo=zone, fold=0)
    late = wall.replace(tzinfo=zone, fold=1)
    if early.utcoffset() != late.utcoffset():
        raise BadLocalTime("ambiguous_time")
    back = early.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if back != wall:
        raise BadLocalTime("nonexistent_time")
    return early.astimezone(UTC)


def offered_of(after: Mapping[str, Any] | None) -> datetime | None:
    """The suggestion offered in a version's `after` data, if any."""
    if after is None or after.get("due_check") not in OFFERED:
        return None
    text = after.get("due_suggested")
    if not isinstance(text, str):
        return None
    return datetime.fromisoformat(text)


def current_due_after(session: Session, item_id: uuid.UUID) -> Mapping[str, Any] | None:
    """The `after` data of the version that set the item's due time."""
    query = (
        select(ItemVersion.after)
        .where(ItemVersion.item_id == item_id, ItemVersion.after.has_key("due_at"))
        .order_by(ItemVersion.version.desc())
        .limit(1)
    )
    return session.execute(query).scalar_one_or_none()


def current_review(session: Session, item_id: uuid.UUID) -> str | None:
    query = (
        select(ItemVersion.after["review"].astext)
        .where(ItemVersion.item_id == item_id, ItemVersion.after.has_key("review"))
        .order_by(ItemVersion.version.desc())
        .limit(1)
    )
    return session.execute(query).scalar_one_or_none()


def source_of(new: datetime, old: datetime | None, offered: datetime | None) -> Source:
    if offered is not None and same_minute(new, offered):
        return "suggestion"
    if old is not None and same_minute(new, old):
        return "parser"
    return "owner"


def iso(when: datetime | None) -> str | None:
    return when.isoformat() if when else None


def audit(item_id: uuid.UUID, version: int, source: Source, status: str) -> AuditEvent:
    return AuditEvent(
        event_type="item.due",
        actor="user",
        subject_type="item",
        subject_id=item_id,
        data={"version": version, "source": source, "status": status},
    )


def set_due(session: Session, item_id: uuid.UUID, due_at: datetime) -> ItemVersion:
    """Set one review item's due time. Does not commit; the caller does.

    The item row is locked first, so a second click finds it open and is
    refused.
    """
    query = select(Item).where(Item.id == item_id).with_for_update()
    item = session.execute(query).scalar_one_or_none()
    if item is None:
        raise UnknownItem(str(item_id))
    review = current_review(session, item_id)
    if not can_set_due(item.kind, item.status, review):
        raise NotReviewable(str(item_id))
    new = due_at.astimezone(UTC)
    offered = offered_of(current_due_after(session, item_id))
    source = source_of(new, item.due_at, offered)
    held = review == UNTRUSTED
    status = "needs_review" if held else "open"
    number = next_version(session, item_id)
    after: dict[str, Any] = {
        "due_at": new.isoformat(),
        "status": status,
        "review": UNTRUSTED if held else None,
        "due_source": source,
        **assumed_fields(NOTHING),
    }
    before = {"due_at": iso(item.due_at), "status": item.status, "review": review}
    version = ItemVersion(
        item_id=item_id,
        version=number,
        change_type="update",
        actor="user",
        before=before,
        after=after,
    )
    item.due_at = new
    item.status = status
    session.add(version)
    session.add(audit(item_id, number, source, status))
    session.flush()
    return version
