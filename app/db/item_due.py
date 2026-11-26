"""Due times the owner sets, and confirming an item (ADR 0022, decision 3).

- `set_due` gives an active deadline or to-do a due time, or changes it. The
  owner enters a local date and an optional time in SINAN_TIMEZONE; with no
  time, 23:59 is used and flagged as assumed (ADR 0016). A time that a DST
  change skips or repeats is refused.
- `confirm` moves an item that needs review, and has a due time, to open.

Each appends an `item_versions` row with actor `user` and updates the item
in the same transaction, plus an audit event with IDs and statuses only.
A new version supersedes any pending item_update proposal for the item,
since validity compares the proposal's base version (ADR 0009, ADR 0014).

Local database writes only, made by the API in answer to the owner. The
model never sets a due time. Functions here do not commit; the caller does.
"""

import uuid
from datetime import UTC, date, datetime, time, tzinfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.item_status import UnknownItem
from app.db.models import AuditEvent, DueSuggestion, Item, ItemVersion
from app.extraction.deadlines import END_OF_DAY, Zone, place
from app.items.assumed import Assumed, assumed_fields
from app.items.changes import current_version
from app.proposals import utc_text

ACTIVE = ("open", "needs_review")
DUE_KINDS = ("deadline", "todo")


class Refused(Exception):
    """The change is not allowed; `code` says why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def local_due(day: date, at: time | None, zone: tzinfo) -> datetime:
    """The UTC due time for a local date and time. Raises Refused."""
    wall = END_OF_DAY if at is None else at
    placed = place(day, wall, Zone(zone, None))
    if isinstance(placed, str):
        raise Refused(placed)
    return placed.astimezone(UTC)


def due_text(day: date, at: time | None) -> str:
    """What the owner entered, as due_raw: "2026-12-01" or "2026-12-01 17:00"."""
    if at is None:
        return day.isoformat()
    return f"{day.isoformat()} {at:%H:%M}"


def lock(session: Session, item_id: uuid.UUID) -> Item:
    query = select(Item).where(Item.id == item_id).with_for_update()
    item = session.execute(query).scalar_one_or_none()
    if item is None:
        raise UnknownItem(str(item_id))
    return item


def audit(kind: str, item: Item, old: str, data: dict[str, object]) -> AuditEvent:
    return AuditEvent(
        event_type=f"item.{kind}",
        actor="user",
        subject_type="item",
        subject_id=item.id,
        data={"from": old, "to": item.status, **data},
    )


def is_suggestion_for(session: Session, suggestion_id: uuid.UUID, item_id: uuid.UUID) -> bool:
    """Whether the ID is a real suggestion made for this item (ADR 0022)."""
    query = select(DueSuggestion.id).where(
        DueSuggestion.id == suggestion_id,
        DueSuggestion.item_id == item_id,
        DueSuggestion.due_at.is_not(None),
    )
    return session.execute(query).first() is not None


def set_due(
    session: Session,
    item_id: uuid.UUID,
    day: date,
    at: time | None,
    zone: tzinfo,
    suggestion_id: uuid.UUID | None = None,
) -> ItemVersion:
    """Set or change an item's due time. Raises UnknownItem or Refused."""
    item = lock(session, item_id)
    if item.kind not in DUE_KINDS:
        raise Refused("notice")
    if item.status not in ACTIVE:
        raise Refused("closed")
    if suggestion_id is not None and not is_suggestion_for(session, suggestion_id, item.id):
        raise Refused("bad_suggestion")
    due_at = local_due(day, at, zone)
    due_raw = due_text(day, at)
    number = current_version(session, item.id) + 1
    old = item.status
    before = {"due_raw": item.due_raw, "due_at": utc_text(item.due_at), "status": old}
    flags = assumed_fields(Assumed(time=at is None))
    after: dict[str, object] = {
        "due_raw": due_raw,
        "due_at": utc_text(due_at),
        "status": "open",
        "review": None,
        **flags,
    }
    data: dict[str, object] = {"version": number}
    if suggestion_id is not None:
        after["suggestion_id"] = str(suggestion_id)
        data["suggestion_id"] = str(suggestion_id)
    version = ItemVersion(
        item_id=item.id,
        version=number,
        change_type="update",
        actor="user",
        before=before,
        after=after,
    )
    item.due_raw = due_raw
    item.due_at = due_at
    item.status = "open"
    session.add(version)
    session.add(audit("due", item, old, data))
    session.flush()
    return version


def confirm(session: Session, item_id: uuid.UUID) -> ItemVersion:
    """Move a needs_review item with a due time to open. Raises on refusal."""
    item = lock(session, item_id)
    if item.status != "needs_review":
        raise Refused("not_review")
    if item.due_at is None:
        raise Refused("no_due")
    number = current_version(session, item.id) + 1
    version = ItemVersion(
        item_id=item.id,
        version=number,
        change_type="status",
        actor="user",
        before={"status": "needs_review"},
        after={"status": "open", "review": None},
    )
    item.status = "open"
    session.add(version)
    session.add(audit("confirm", item, "needs_review", {"version": number}))
    session.flush()
    return version
