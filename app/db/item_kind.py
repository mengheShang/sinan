"""Make a notice a to-do, from the Dashboard (ADR 0017, decision 3).

This is how the owner fixes a wrong demotion. The only allowed move is
`notice` -> `todo`; the due time, title and status stay as they are.

The change appends an `item_versions` row with actor `user` and updates
`items.kind` in the same transaction, plus an audit event with IDs and
kinds only. Local database writes only: nothing reaches Gmail or any other
service, and the model plays no part.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.item_status import UnknownItem, next_version
from app.db.models import AuditEvent, Item, ItemVersion

FROM = "notice"
TO = "todo"


class NotANotice(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def audit(item_id: uuid.UUID, version: int) -> AuditEvent:
    return AuditEvent(
        event_type="item.kind",
        actor="user",
        subject_type="item",
        subject_id=item_id,
        data={"from": FROM, "to": TO, "version": version},
    )


def make_todo(session: Session, item_id: uuid.UUID) -> ItemVersion:
    """Turn one notice into a to-do. Does not commit; the caller does.

    The item row is locked first, so two clicks cannot pick the same
    version number, and the second one finds a to-do and is refused.
    """
    query = select(Item).where(Item.id == item_id).with_for_update()
    item = session.execute(query).scalar_one_or_none()
    if item is None:
        raise UnknownItem(str(item_id))
    if item.kind != FROM:
        raise NotANotice(item.kind)
    number = next_version(session, item_id)
    version = ItemVersion(
        item_id=item_id,
        version=number,
        change_type="update",
        actor="user",
        before={"kind": FROM},
        after={"kind": TO},
    )
    item.kind = TO
    session.add(version)
    session.add(audit(item_id, number))
    session.flush()
    return version
