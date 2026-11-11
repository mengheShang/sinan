"""Status changes made by the owner from the Dashboard (Issue #29).

Allowed moves:

- open or needs_review -> done or dismissed
- done or dismissed -> open

Each change appends an `item_versions` row with actor `user` and updates
`items.status` in the same transaction, plus an audit event with IDs and
statuses only. Local database writes only: nothing reaches Gmail or any
other service, and the model plays no part.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import AuditEvent, Item, ItemVersion

CLOSED = ("done", "dismissed")
MOVES: dict[str, tuple[str, ...]] = {
    "open": CLOSED,
    "needs_review": CLOSED,
    "done": ("open",),
    "dismissed": ("open",),
}


class UnknownItem(Exception):
    pass


class BadMove(Exception):
    def __init__(self, old: str, new: str) -> None:
        super().__init__(f"{old} -> {new}")
        self.old = old
        self.new = new


def allowed(old: str, new: str) -> bool:
    return new in MOVES.get(old, ())


def next_version(session: Session, item_id: uuid.UUID) -> int:
    query = select(func.max(ItemVersion.version)).where(ItemVersion.item_id == item_id)
    last = session.execute(query).scalar_one()
    return (last or 0) + 1


def audit(item_id: uuid.UUID, old: str, new: str, version: int) -> AuditEvent:
    return AuditEvent(
        event_type="item.status",
        actor="user",
        subject_type="item",
        subject_id=item_id,
        data={"from": old, "to": new, "version": version},
    )


def set_status(session: Session, item_id: uuid.UUID, new: str) -> ItemVersion:
    """Apply one status change. Does not commit; the caller does.

    The item row is locked first, so two changes to the same item cannot
    pick the same version number.
    """
    query = select(Item).where(Item.id == item_id).with_for_update()
    item = session.execute(query).scalar_one_or_none()
    if item is None:
        raise UnknownItem(str(item_id))
    old = item.status
    if not allowed(old, new):
        raise BadMove(old, new)
    number = next_version(session, item_id)
    version = ItemVersion(
        item_id=item_id,
        version=number,
        change_type="status",
        actor="user",
        before={"status": old},
        after={"status": new},
    )
    item.status = new
    session.add(version)
    session.add(audit(item_id, old, new, number))
    session.flush()
    return version
