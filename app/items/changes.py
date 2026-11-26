"""Turn an extracted deadline change into an item_update proposal.

Called by extraction for an item the model marked as an update and whose due
time parsed (ADR 0009, decisions 2 and 6; technical design "DDL changes"):

- The candidates are the active items (open or needs_review) from other
  emails, except notices (ADR 0017), matched by app.items.matching. Exactly
  one candidate gets a pending proposal based on its current version.
- Several or none: when a link picker is given, the model is asked once to
  pick from a list built by app.items.matching.choices (ADR 0019). A pick
  takes the same path as a deterministic match, so it only ever becomes a
  pending proposal. No pick: the caller saves the change as a new item
  marked needs_review.
- An older email never overrides a newer one. When the target's due time
  came from a newer email, or a pending proposal for it did, the change is
  dropped and counted as stale. A change from a newer email supersedes the
  target's pending proposals from older emails.
- A change that gives a date but no time keeps the time of day of the
  item's current due time (app/items/carry.py, ADR 0016 decision 7).
- A change that gives the time the item already has proposes nothing (it
  still supersedes older pending proposals, since it confirms that time).

ADR 0023 adds two paths. `find_repeat` matches an item the model called
new: the same title words link it without the model, a similar title asks
the picker with the repeat prompt. `mark_unparsed` flags the target of a
change whose due time did not parse, keeping its due time.

Only local rows are written: the proposal and audit events with IDs,
statuses and counts. The creation event says which path linked the change
(`deterministic` or `llm`) and how many candidates that path had. Nothing
is applied here; the owner decides in the Dashboard.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, Literal

from sqlalchemy import Subquery, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import AuditEvent, ExtractionRun, Item, ItemVersion, Message, Proposal
from app.extraction.link_prompt import (
    LINK_PROMPT_HASH,
    LINK_PROMPT_VERSION,
    REPEAT_PROMPT_HASH,
    REPEAT_PROMPT_VERSION,
)
from app.extraction.prompt import prepare
from app.extraction.validate import ValidItem
from app.items.assumed import Assumed, assumed_of
from app.items.carry import carry_time
from app.items.linking import Pick, ask
from app.items.matching import MAX_CHOICES, Known, candidates, choices, match, same_title
from app.proposals import DETERMINISTIC, ITEM_UPDATE, expiry, item_update, payload_hash
from app.proposals import Link as Path
from app.providers.llm import LinkPicker

ACTIVE = ("open", "needs_review")

Result = Literal["proposed", "unchanged", "stale", "several", "none", "marked"]
# Review code for an item a later email changed with a due time that did
# not parse (ADR 0023, decision 2)
UNPARSED = "update_unparsed"


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


@dataclass(frozen=True)
class Link:
    """How a change found its target, in IDs and counts only."""

    target: uuid.UUID | None
    path: Path | None
    # Deterministic candidates found
    matched: int
    # The model's answer, when it was asked
    pick: Pick[uuid.UUID] | None = None
    model: str | None = None
    # The model was asked whether a new item repeats one (ADR 0023)
    repeat: bool = False

    def audit_data(self) -> dict[str, Any]:
        """The creation event's fields for this link. Never any text."""
        if self.path != "llm":
            return {"link": "deterministic", "candidates": self.matched}
        shown = self.pick.shown if self.pick else 0
        return {
            "link": "llm",
            "candidates": shown,
            "matched": self.matched,
            "link_model": self.model,
            "link_prompt_version": REPEAT_PROMPT_VERSION if self.repeat else LINK_PROMPT_VERSION,
            "link_prompt_hash": REPEAT_PROMPT_HASH if self.repeat else LINK_PROMPT_HASH,
        }


def seen_times() -> Subquery:
    """Per item, the newest email date behind its created or changed time."""
    newest = func.max(message_time()).label("seen_at")
    query = (
        select(ItemVersion.item_id, newest)
        .join(Message, Message.id == ItemVersion.message_id)
        .where(ItemVersion.change_type.in_(("create", "update")))
        .group_by(ItemVersion.item_id)
    )
    return query.subquery()


def pool(session: Session, msg_id: uuid.UUID) -> list[Known[uuid.UUID]]:
    """Active items except notices and those first found in this same email."""
    same = select(ItemVersion.item_id).where(
        ItemVersion.version == 1,
        ItemVersion.message_id == msg_id,
    )
    seen = seen_times()
    query = (
        select(Item.id, Item.course, Item.title, Item.due_at, seen.c.seen_at)
        .outerjoin(seen, seen.c.item_id == Item.id)
        .where(Item.status.in_(ACTIVE), Item.kind != "notice", Item.id.not_in(same))
        .order_by(Item.created_at, Item.id)
    )
    rows = session.execute(query).all()
    return [Known(row[0], row[1], row[2], row[3], row[4]) for row in rows]


def email_text(source: Source) -> str:
    """The fenced email, as extraction showed it to the model."""
    msg = source.message
    return prepare(msg.subject, msg.from_addr, source.sent_at, msg.body_text or "")


def find(session: Session, item: ValidItem, source: Source, picker: LinkPicker | None) -> Link:
    """The deterministic match, else the model's pick when a picker is given."""
    known = pool(session, source.message.id)
    found = match(item.course, item.title, known)
    if found.outcome == "one":
        return Link(found.target, "deterministic", found.candidates)
    if picker is None:
        return Link(None, None, found.candidates)
    options = choices(item.course, source.sent_at, known)
    picked = ask(picker.pick, email_text(source), item, options)
    path: Path | None = "llm" if picked.target is not None else None
    return Link(picked.target, path, found.candidates, picked, picker.model)


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
        .where(Proposal.kind == ITEM_UPDATE)
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


def find_repeat(
    session: Session, item: ValidItem, source: Source, picker: LinkPicker | None
) -> Link:
    """The existing item a new item repeats, if any (ADR 0023, decision 4).

    One candidate with the same title words is the item, with no model call.
    Otherwise, when the matcher finds candidates and a picker is given, the
    model is asked once whether the item is one of them. No candidate, no
    picker, or no pick: no target, so the item is saved as new.
    """
    known = pool(session, source.message.id)
    found = candidates(item.course, item.title, known)
    if not found:
        return Link(None, None, 0)
    exact = [k for k in found if same_title(item.course, item.title, k)]
    if len(exact) == 1:
        return Link(exact[0].key, "deterministic", len(found))
    if picker is None:
        return Link(None, None, len(found))
    options = found[-MAX_CHOICES:][::-1]
    picked = ask(picker.pick, email_text(source), item, options, repeat=True)
    path: Path | None = "llm" if picked.target is not None else None
    return Link(picked.target, path, len(found), picked, picker.model, repeat=True)


def create(
    session: Session,
    item: ValidItem,
    target: Item,
    source: Source,
    timing: tuple[datetime, int],
    link: Link,
    new_item: dict[str, Any] | None = None,
) -> Proposal:
    """A pending proposal. `timing` is the creation time and the TTL in days."""
    now, ttl = timing
    base = current_version(session, target.id)
    before = (target.due_raw, target.due_at)
    after = (item.due_raw, item.due_at)
    old = current_assumed(session, target.id)
    new = Assumed(item.zone_assumed, item.time_assumed, item.date_computed, item.time_carried)
    path = link.path or DETERMINISTIC
    payload = item_update(str(target.id), before, after, item.evidence, old, new, path, new_item)
    proposal = Proposal(
        kind=ITEM_UPDATE,
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
    data = {"to": "pending", "base_version": base, **link.audit_data()}
    session.add(event("created", proposal, "worker", data))
    return proposal


def propose(
    session: Session,
    item: ValidItem,
    source: Source,
    now: datetime,
    ttl: int,
    home: tzinfo,
    picker: LinkPicker | None = None,
    new_item: dict[str, Any] | None = None,
    linked: Link | None = None,
) -> tuple[Result, Link]:
    """Link one extracted change and propose it when it has a target.

    `home` is SINAN_TIMEZONE, for reading the item's zone when its due time
    names none. `picker` is the link step (ADR 0019); without it only the
    deterministic matcher links. `new_item` is set for an item the model
    called new (ADR 0023): the proposal keeps it, so rejecting saves it.
    `linked` is a link already found, which skips the lookup.
    """
    link = linked if linked is not None else find(session, item, source, picker)
    if link.target is None:
        unlinked: Result = "several" if link.matched > 1 else "none"
        return unlinked, link
    lock = select(Item).where(Item.id == link.target).with_for_update()
    target = session.execute(lock).scalar_one()
    if is_stale(session, target.id, source.sent_at):
        return "stale", link
    supersede_older(session, target.id, source.sent_at)
    flags = current_assumed(session, target.id)
    item = carry_time(item, target.due_raw, target.due_at, flags, home)
    if target.due_at == item.due_at:
        return "unchanged", link
    create(session, item, target, source, (now, ttl), link, new_item)
    return "proposed", link


def mark_unparsed(session: Session, item: ValidItem, source: Source, link: Link) -> Result:
    """Flag the linked item for review: a later email changed its due time,
    but the new one did not parse (ADR 0023, decision 2).

    The item keeps its due time. The version points at the new email and
    its evidence, so the Dashboard shows the quote with the flag. An older
    email changes nothing. `link.target` must be set.
    """
    if link.target is None:
        raise ValueError("mark_unparsed needs a target")
    lock = select(Item).where(Item.id == link.target).with_for_update()
    target = session.execute(lock).scalar_one()
    if is_stale(session, target.id, source.sent_at):
        return "stale"
    supersede_older(session, target.id, source.sent_at)
    number = current_version(session, target.id) + 1
    old = target.status
    after = {"status": "needs_review", "review": UNPARSED, "new_due_raw": item.due_raw}
    version = ItemVersion(
        item_id=target.id,
        version=number,
        change_type="status",
        actor="extraction",
        before={"status": old},
        after=after,
        evidence=item.evidence,
        message_id=source.message.id,
        extraction_run_id=source.run.id,
    )
    target.status = "needs_review"
    session.add(version)
    data = {"from": old, "to": "needs_review", "version": number, "review": UNPARSED}
    audit = AuditEvent(
        event_type="item.status",
        actor="worker",
        subject_type="item",
        subject_id=target.id,
        data={**data, **link.audit_data()},
    )
    session.add(audit)
    session.flush()
    return "marked"
