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

A digest also counts the proposals that expired unanswered since the
previous digest (ADR 0009, decision 6): those whose expiry time falls after
the previous digest and no later than this one. The Worker checks proposals
right before making a digest, so the count and the needs_review section
include them.

Time on read (ADR 0015): each active item has a phase from
app.items.timing. A digest stores only the items that were current when it
was made. Reading it takes a clock: stored items that have since become
overdue, past or stale leave their sections, and the "overdue" group is
computed from all active items at that moment. It is never stored, so a
digest's rows never change after it is made. Past and stale items are
listed by `past_items` for the Past view.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, get_args
from zoneinfo import ZoneInfo

from sqlalchemy import Row, Select, func, select
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.orm import Session

from app.db.models import (
    Account,
    AuditEvent,
    Digest,
    DigestItem,
    Item,
    ItemVersion,
    Message,
    Proposal,
)
from app.items.timing import Phase, phase

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


def pending_items(session: Session, now: datetime) -> set[uuid.UUID]:
    """Items with a proposal that can still be decided at `now` (ADR 0014).

    The same test as app.proposals.validity: pending, not expired, and based
    on the item's current version.
    """
    newest_version = func.max(ItemVersion.version).label("version")
    versions = select(ItemVersion.item_id, newest_version).group_by(ItemVersion.item_id)
    current = versions.subquery()
    query = (
        select(Proposal.item_id)
        .join(current, current.c.item_id == Proposal.item_id)
        .where(Proposal.status == "pending")
        .where(Proposal.expires_at > now)
        .where(Proposal.base_version == current.c.version)
    )
    return set(session.execute(query).scalars())


def expired_between(session: Session, since: datetime | None, until: datetime) -> int:
    """Proposals that expired unanswered after `since`, up to `until`."""
    query = select(func.count()).where(
        Proposal.status == "expired",
        Proposal.expires_at <= until,
    )
    if since is not None:
        query = query.where(Proposal.expires_at > since)
    return int(session.execute(query).scalar_one())


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
    found = section_ids(session, now, since)
    sections = keep_current(session, found, now)
    for section, ids in sections.items():
        for item_id in ids:
            session.add(DigestItem(digest_id=digest_id, section=section, item_id=item_id))
    counts = {name: len(ids) for name, ids in sections.items()}
    counts["expired_proposals"] = expired_between(session, since, now)
    session.add(audit(digest_id, today, counts))
    session.commit()
    return session.get(Digest, digest_id)


def keep_current(
    session: Session, sections: dict[str, list[uuid.UUID]], now: datetime
) -> dict[str, list[uuid.UUID]]:
    """Drop items that are not current at `now`, so the digest stores none.

    This also keeps an item that was already past when it was extracted out
    of "new" (ADR 0015, decision 3).
    """
    ids = {item_id for ids in sections.values() for item_id in ids}
    rows = session.execute(item_query().where(Item.id.in_(ids))).all() if ids else []
    pending = pending_items(session, now)
    phases = {view.id: phase_of(view, now, pending) for view in map(to_view, rows)}
    kept: dict[str, list[uuid.UUID]] = {}
    for name, section in sections.items():
        kept[name] = [item_id for item_id in section if phases[item_id] == "current"]
    return kept


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
    # Active items whose due time passed less than two days ago, at read time
    overdue: list[ItemView]
    # Proposals that expired unanswered since the previous digest
    expired_proposals: int


def newest(condition: Any) -> Any:
    """Each item's newest version that meets `condition`, as a subquery."""
    query = (
        select(ItemVersion.item_id, ItemVersion.after, ItemVersion.evidence, ItemVersion.message_id)
        .where(condition)
        .ext(distinct_on(ItemVersion.item_id))
        .order_by(ItemVersion.item_id, ItemVersion.version.desc())
    )
    return query.subquery()


def item_query() -> Select[Any, Any, Any, Any, Any]:
    """Items with their latest review code, evidence and source message.

    The review code comes from the newest version that sets one, and the
    evidence and message from the newest version with evidence: the email
    that created the item, or the one behind its latest change.
    """
    review = newest(ItemVersion.after.has_key("review"))
    source = newest(ItemVersion.evidence.is_not(None))
    return (
        select(Item, review.c.after, source.c.evidence, Message, Account.email)
        .outerjoin(review, review.c.item_id == Item.id)
        .outerjoin(source, source.c.item_id == Item.id)
        .outerjoin(Message, Message.id == source.c.message_id)
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


def phase_of(view: ItemView, now: datetime, pending: set[uuid.UUID]) -> Phase:
    source_at = view.received_at or view.created_at
    return phase(view.status, view.due_at, source_at, now, view.id in pending)


def shown(view: ItemView, now: datetime, pending: set[uuid.UUID]) -> bool:
    """Whether a stored item still belongs in its section at `now`.

    Closed items stay, so the Dashboard can count what was closed since the
    digest was made; overdue ones move to the overdue group.
    """
    if view.status not in ACTIVE:
        return True
    return phase_of(view, now, pending) == "current"


def by_phase(session: Session, now: datetime) -> dict[Phase, list[ItemView]]:
    """Active items grouped by their phase at `now`, each in digest order."""
    pending = pending_items(session, now)
    groups: dict[Phase, list[ItemView]] = {p: [] for p in get_args(Phase)}
    for view in list_items(session, ACTIVE):
        groups[phase_of(view, now, pending)].append(view)
    return groups


def read_digest(session: Session, digest: Digest, now: datetime) -> DigestView:
    """A stored digest as it stands at `now`; the stored rows are not changed."""
    links = select(DigestItem.section, DigestItem.item_id).where(DigestItem.digest_id == digest.id)
    pairs = session.execute(links).all()
    ids = {item_id for _, item_id in pairs}
    rows = session.execute(item_query().where(Item.id.in_(ids))).all() if ids else []
    views = {view.id: view for view in map(to_view, rows)}
    pending = pending_items(session, now)
    sections: dict[str, list[ItemView]] = {name: [] for name in SECTIONS}
    for section, item_id in pairs:
        view = views[item_id]
        if shown(view, now, pending):
            sections[section].append(view)
    for name in SECTIONS:
        sections[name].sort(key=order_key)
    expired = expired_between(session, digest.since, digest.generated_at)
    overdue = by_phase(session, now)["overdue"]
    return DigestView(
        digest.id,
        digest.digest_date,
        digest.generated_at,
        digest.since,
        sections,
        overdue,
        expired,
    )


def list_items(session: Session, statuses: tuple[str, ...]) -> list[ItemView]:
    rows = session.execute(item_query().where(Item.status.in_(statuses))).all()
    views = [to_view(row) for row in rows]
    views.sort(key=order_key)
    return views


@dataclass(frozen=True)
class PastView:
    # Due time passed two or more days ago
    past: list[ItemView]
    # Needs review, no due time, source email over 14 days old
    stale: list[ItemView]


def past_items(session: Session, now: datetime) -> PastView:
    """Active items that have left the digest at `now`. Their status is unchanged."""
    groups = by_phase(session, now)
    # Most recently due first: the ones the owner may still act on lead
    past = groups["past"][::-1]
    return PastView(past, groups["stale"])
