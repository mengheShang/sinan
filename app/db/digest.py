"""Make and read the daily digest (ADR 0008).

The Worker makes one digest per local day, on its first cycle at or after
SINAN_DIGEST_HOUR in SINAN_TIMEZONE. A missed morning is caught up on the
next cycle. Demo mode ignores the hour, so the demo has a digest at once.

Sections, over items that are open or need review:

- new: created after the previous digest (every item for the first one)
- due_soon: due within 7 days of when the digest was made
- needs_review: marked for review

`digest_items` stores only which items were in which section. Reading a
digest joins to `items`, so the Dashboard shows each item's current state.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Row, Select, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Account, AuditEvent, Digest, DigestItem, Item, ItemVersion, Message

DUE_WINDOW = timedelta(days=7)
ACTIVE = ("open", "needs_review")
SECTIONS = ("new", "due_soon", "needs_review")


def local_date(now: datetime, zone: ZoneInfo) -> date:
    return now.astimezone(zone).date()


def is_due(now: datetime, zone: ZoneInfo, hour: int, force: bool) -> bool:
    """Whether today's digest may be made now: at or after the hour, or forced."""
    return force or now.astimezone(zone).hour >= hour


def latest(session: Session) -> Digest | None:
    query = select(Digest).order_by(Digest.digest_date.desc()).limit(1)
    return session.execute(query).scalar_one_or_none()


def section_ids(
    session: Session, now: datetime, since: datetime | None
) -> dict[str, list[uuid.UUID]]:
    active = Item.status.in_(ACTIVE)
    new_q = select(Item.id).where(active)
    if since is not None:
        new_q = new_q.where(Item.created_at > since)
    soon_q = select(Item.id).where(
        active,
        Item.due_at >= now,
        Item.due_at < now + DUE_WINDOW,
    )
    review_q = select(Item.id).where(Item.status == "needs_review")
    return {
        "new": list(session.execute(new_q).scalars()),
        "due_soon": list(session.execute(soon_q).scalars()),
        "needs_review": list(session.execute(review_q).scalars()),
    }


def make_digest(
    session: Session, now: datetime, zone: ZoneInfo, hour: int, force: bool = False
) -> Digest | None:
    """Make today's digest if it is due and missing. Commits; returns it or None."""
    today = local_date(now, zone)
    if not is_due(now, zone, hour, force):
        return None
    previous = latest(session)
    if previous is not None and previous.digest_date >= today:
        return None
    since = previous.generated_at if previous is not None else None
    stmt = insert(Digest).values(digest_date=today, generated_at=now, since=since)
    stmt = stmt.on_conflict_do_nothing(index_elements=["digest_date"])
    digest_id = session.execute(stmt.returning(Digest.id)).scalar_one_or_none()
    if digest_id is None:
        # Another run made it first
        session.rollback()
        return None
    sections = section_ids(session, now, since)
    for section, ids in sections.items():
        for item_id in ids:
            session.add(DigestItem(digest_id=digest_id, section=section, item_id=item_id))
    counts = {name: len(ids) for name, ids in sections.items()}
    session.add(audit(digest_id, today, counts))
    session.commit()
    return session.get(Digest, digest_id)


def audit(digest_id: uuid.UUID, day: date, counts: dict[str, int]) -> AuditEvent:
    return AuditEvent(
        event_type="digest",
        actor="worker",
        subject_type="digest",
        subject_id=digest_id,
        data={"date": day.isoformat(), **counts},
    )


# ---- Reading ----


@dataclass(frozen=True)
class ItemView:
    id: uuid.UUID
    kind: str
    course: str | None
    title: str
    due_raw: str | None
    due_at: datetime | None
    status: str
    review: str | None
    evidence: str | None
    created_at: datetime
    subject: str | None
    sender: str | None
    received_at: datetime | None
    account_email: str | None
    provider_msg_id: str | None


@dataclass(frozen=True)
class DigestView:
    id: uuid.UUID
    digest_date: date
    generated_at: datetime
    since: datetime | None
    sections: dict[str, list[ItemView]]


def item_query() -> Select[Any, Any, Any, Any, Any]:
    """Items with their first version's evidence and source message."""
    first = select(ItemVersion).where(ItemVersion.version == 1).subquery()
    return (
        select(Item, first.c.after, first.c.evidence, Message, Account.email)
        .outerjoin(first, first.c.item_id == Item.id)
        .outerjoin(Message, Message.id == first.c.message_id)
        .outerjoin(Account, Account.id == Message.account_id)
    )


def to_view(row: Row[Any, Any, Any, Any, Any]) -> ItemView:
    item: Item = row[0]
    after: dict[str, Any] | None = row[1]
    evidence: str | None = row[2]
    msg: Message | None = row[3]
    email: str | None = row[4]
    review = (after or {}).get("review")
    return ItemView(
        id=item.id,
        kind=item.kind,
        course=item.course,
        title=item.title,
        due_raw=item.due_raw,
        due_at=item.due_at,
        status=item.status,
        review=review if isinstance(review, str) else None,
        evidence=evidence,
        created_at=item.created_at,
        subject=msg.subject if msg else None,
        sender=msg.from_addr if msg else None,
        received_at=msg.received_at if msg else None,
        account_email=email,
        provider_msg_id=msg.provider_msg_id if msg else None,
    )


def order_key(view: ItemView) -> tuple[bool, datetime, str]:
    """Soonest due first; items without a due time last, in the order they were found."""
    when = view.due_at or view.created_at
    return (view.due_at is None, when, view.title)


def read_digest(session: Session, digest: Digest) -> DigestView:
    links = select(DigestItem.section, DigestItem.item_id).where(DigestItem.digest_id == digest.id)
    pairs = session.execute(links).all()
    ids = {item_id for _, item_id in pairs}
    rows = session.execute(item_query().where(Item.id.in_(ids))).all() if ids else []
    views = {view.id: view for view in map(to_view, rows)}
    sections: dict[str, list[ItemView]] = {name: [] for name in SECTIONS}
    for section, item_id in pairs:
        sections[section].append(views[item_id])
    for name in SECTIONS:
        sections[name].sort(key=order_key)
    return DigestView(digest.id, digest.digest_date, digest.generated_at, digest.since, sections)


def list_items(session: Session, statuses: tuple[str, ...]) -> list[ItemView]:
    rows = session.execute(item_query().where(Item.status.in_(statuses))).all()
    views = [to_view(row) for row in rows]
    views.sort(key=order_key)
    return views
