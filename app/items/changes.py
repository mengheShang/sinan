"""Turn an extracted deadline change into an item_update proposal.

Called by extraction for an item the model marked as an update and whose due
time parsed (ADR 0009, decisions 2 and 6; technical design "DDL changes"):

- The candidates are the active items (open or needs_review) from other
  emails, except notices (ADR 0017), matched by app.items.matching. Exactly one candidate gets a
  pending proposal based on its current version. Several or none: the
  caller saves the change as a new item marked needs_review.
- An older email never overrides a newer one. When the target's due time
  came from a newer email, or a pending proposal for it did, the change is
  dropped and counted as stale. A change from a newer email supersedes the
  target's pending proposals from older emails.
- A change that gives a date but no time keeps the time of day of the
  item's current due time (app/items/carry.py, ADR 0016 decision 7).
- A change that gives the time the item already has proposes nothing (it
  still supersedes older pending proposals, since it confirms that time).

Only local rows are written: the proposal and audit events with IDs and
statuses. Nothing is applied here; the owner decides in the Dashboard.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import AuditEvent, ExtractionRun, Item, ItemVersion, Message, Proposal
from app.extraction.validate import ValidItem
from app.items.assumed import Assumed, assumed_of
from app.items.carry import carry_time
from app.items.matching import Known, match
from app.proposals import expiry, item_update, payload_hash

ACTIVE = ("open", "needs_review")

Result = Literal["proposed", "unchanged", "stale", "several", "none"]


@dataclass(frozen=True)
class Source:
    """The email and run a change came from."""

    message: Message
    run: ExtractionRun
    # The email's date: the Date header, else when it was received
    sent_at: datetime


def message_time() -> ColumnElement[datetime]:
    """SQL for a message's date: the Date header, else when it was received."""
    return func.coalesce(Message.sent_at, Message.received_at)


def pool(session: Session, msg_id: uuid.UUID) -> list[Known[uuid.UUID]]:
    """Active items except notices and those first found in this same email."""
    same = select(ItemVersion.item_id).where(
        ItemVersion.version == 1,
        ItemVersion.message_id == msg_id,
    )
    query = (
        select(Item.id, Item.course, Item.title)
        .where(Item.status.in_(ACTIVE), Item.kind != "notice", Item.id.not_in(same))
        .order_by(Item.created_at, Item.id)
    )
    rows = session.execute(query).all()
    return [Known(row[0], row[1], row[2]) for row in rows]


def current_version(session: Session, item_id: uuid.UUID) -> int:
    query = select(func.max(ItemVersion.version)).where(ItemVersion.item_id == item_id)
    last = session.execute(query).scalar_one()
    return int(last or 0)


def confirmed_at(session: Session, item_id: uuid.UUID) -> datetime | None:
    """The newest email date behind the item's created or changed time."""
    query = (
        select(func.max(message_time()))
        .join(ItemVersion, ItemVersion.message_id == Message.id)
        .where(
            ItemVersion.item_id == item_id,
            ItemVersion.change_type.in_(("create", "update")),
        )
    )
    return session.execute(query).scalar_one()


def current_assumed(session: Session, item_id: uuid.UUID) -> Assumed:
    """The assumed parts of the item's due time, from the version that set it."""
    query = (
        select(ItemVersion.after)
        .where(ItemVersion.item_id == item_id, ItemVersion.after.has_key("due_at"))
        .order_by(ItemVersion.version.desc())
        .limit(1)
    )
    after = session.execute(query).scalar_one_or_none()
    return assumed_of(after)


def pending_on(session: Session, item_id: uuid.UUID) -> list[tuple[Proposal, datetime]]:
    """The item's pending proposals, each with its email's date."""
    query = (
        select(Proposal, message_time())
        .join(Message, Message.id == Proposal.message_id)
        .where(Proposal.item_id == item_id, Proposal.status == "pending")
        .order_by(Proposal.created_at)
    )
    return [(row[0], row[1]) for row in session.execute(query).all()]


def is_stale(session: Session, item_id: uuid.UUID, sent_at: datetime) -> bool:
    confirmed = confirmed_at(session, item_id)
    if confirmed is not None and confirmed > sent_at:
        return True
    return any(when > sent_at for _, when in pending_on(session, item_id))


def event(kind: str, proposal: Proposal, actor: str, data: Mapping[str, object]) -> AuditEvent:
    return AuditEvent(
        event_type=f"proposal.{kind}",
        actor=actor,
        subject_type="proposal",
        subject_id=proposal.id,
        data={"item_id": str(proposal.item_id), **data},
    )


def supersede_older(session: Session, item_id: uuid.UUID, sent_at: datetime) -> None:
    for proposal, when in pending_on(session, item_id):
        if when < sent_at:
            proposal.status = "superseded"
            data = {"from": "pending", "to": "superseded", "reason": "newer_email"}
            session.add(event("superseded", proposal, "worker", data))


def create(
    session: Session, item: ValidItem, target: Item, source: Source, now: datetime, ttl: int
) -> Proposal:
    base = current_version(session, target.id)
    before = (target.due_raw, target.due_at)
    after = (item.due_raw, item.due_at)
    old = current_assumed(session, target.id)
    new = Assumed(item.zone_assumed, item.time_assumed, item.date_computed, item.time_carried)
    payload = item_update(str(target.id), before, after, item.evidence, old, new)
    proposal = Proposal(
        kind="item_update",
        item_id=target.id,
        base_version=base,
        payload=payload,
        payload_hash=payload_hash(payload),
        message_id=source.message.id,
        extraction_run_id=source.run.id,
        expires_at=expiry(now, item.due_at, ttl),
    )
    session.add(proposal)
    session.flush()
    data = {"to": "pending", "base_version": base}
    session.add(event("created", proposal, "worker", data))
    return proposal


def propose(
    session: Session, item: ValidItem, source: Source, now: datetime, ttl: int, home: tzinfo
) -> Result:
    """Match one extracted change and propose it when there is one target.

    `home` is SINAN_TIMEZONE, for reading the item's zone when its due time
    names none.
    """
    found = match(item.course, item.title, pool(session, source.message.id))
    if found.outcome != "one" or found.target is None:
        return "several" if found.outcome == "several" else "none"
    lock = select(Item).where(Item.id == found.target).with_for_update()
    target = session.execute(lock).scalar_one()
    if is_stale(session, target.id, source.sent_at):
        return "stale"
    supersede_older(session, target.id, source.sent_at)
    flags = current_assumed(session, target.id)
    item = carry_time(item, target.due_raw, target.due_at, flags, home)
    if target.due_at == item.due_at:
        return "unchanged"
    create(session, item, target, source, now, ttl)
    return "proposed"
