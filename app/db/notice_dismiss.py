"""Dismiss the notices shown on a Dashboard page (ADR 0017, change B).

The form sends the IDs of the notices it showed, so a notice that arrives
between loading the page and clicking stays undismissed. An ID that is not
an open notice (unknown, a to-do, already dismissed) is skipped.

Each notice is dismissed through `set_status`, the same path as a single
Dismiss: one `item_versions` row with actor `user` and one `item.status`
audit event each. One more audit event records the batch with IDs and
counts only. Local database writes only: nothing reaches Gmail.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.item_status import set_status
from app.db.models import AuditEvent, Item

NOTICE = "notice"
OPEN = "open"
# More IDs than any page shows; the API refuses a longer list
MAX_IDS = 500


def open_notices(session: Session, ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """The given IDs that are open notices, locked, in a fixed order."""
    query = (
        select(Item.id)
        .where(Item.id.in_(ids), Item.kind == NOTICE, Item.status == OPEN)
        .order_by(Item.id)
        .with_for_update()
    )
    return list(session.execute(query).scalars())


def audit(sent: int, done: list[uuid.UUID]) -> AuditEvent:
    return AuditEvent(
        event_type="notice.dismiss_all",
        actor="user",
        subject_type=None,
        subject_id=None,
        data={"sent": sent, "dismissed": len(done), "ids": [str(i) for i in done]},
    )


def dismiss_all(session: Session, ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Dismiss the open notices among `ids`. Does not commit; the caller does.

    Returns the IDs dismissed. The rows are locked in ID order, so two
    batches cannot deadlock or dismiss the same notice twice.
    """
    unique = sorted(set(ids))
    found = open_notices(session, unique) if unique else []
    for item_id in found:
        set_status(session, item_id, "dismissed")
    session.add(audit(len(unique), found))
    session.flush()
    return found
