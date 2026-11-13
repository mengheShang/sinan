"""Decide item_update proposals: approve, reject, expire and supersede.

ADR 0009, decisions 2 and 6. Every path checks a proposal with the same
function, app.proposals.validity, both when proposals are read (`sweep`)
and right before one is applied (`approve`):

- superseded: the target item has a newer version than the proposal's
  `base_version`, from another email or from the owner.
- expired: `expires_at` has passed. The item moves to needs_review, because
  a missed deadline is worse than an extra prompt.

Approving recomputes `payload_hash` from the stored payload and refuses a
mismatch, and also refuses when the hash the owner was shown differs. Then,
in one transaction, it appends an `item_versions` row (`change_type =
'update'`), updates the item, and marks the proposal approved and executed.

Local database writes only. Nothing reaches Gmail or any other service, and
the model plays no part. Audit events hold IDs and statuses only. Functions
here do not commit; the caller does.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditEvent, Item, ItemVersion, Proposal
from app.items.changes import current_version, event
from app.proposals import EXPIRED, SUPERSEDED, VALID, parse_utc, payload_hash, validity

ACTIVE = ("open", "needs_review")
REVIEW_CODE = "proposal_expired"


class UnknownProposal(Exception):
    pass


class NotPending(Exception):
    """The proposal can no longer be decided; `status` says what it is now."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


class HashMismatch(Exception):
    """The stored payload, its hash and the hash shown to the owner disagree."""


def lock_item(session: Session, item_id: uuid.UUID) -> Item:
    query = select(Item).where(Item.id == item_id).with_for_update()
    return session.execute(query).scalar_one()


def mark(session: Session, proposal: Proposal, status: str, actor: str) -> None:
    session.add(event(status, proposal, actor, {"from": proposal.status, "to": status}))
    proposal.status = status
    session.flush()


def to_review(session: Session, proposal: Proposal, actor: str) -> None:
    """Move an expired proposal's item to needs_review, as a new version.

    The version points at the email whose change went unanswered, so the
    Dashboard can show that evidence next to the review flag.
    """
    item = lock_item(session, proposal.item_id)
    if item.status != "open":
        # Already in review, or closed: nothing to flag
        return
    number = current_version(session, item.id) + 1
    version = ItemVersion(
        item_id=item.id,
        version=number,
        change_type="status",
        actor="extraction",
        before={"status": item.status},
        after={"status": "needs_review", "review": REVIEW_CODE, "proposal_id": str(proposal.id)},
        evidence=proposal.payload.get("evidence"),
        message_id=proposal.message_id,
        extraction_run_id=proposal.extraction_run_id,
    )
    item.status = "needs_review"
    session.add(version)
    data = {"from": "open", "to": "needs_review", "version": number}
    session.add(
        AuditEvent(
            event_type="item.status",
            actor=actor,
            subject_type="item",
            subject_id=item.id,
            data={**data, "proposal_id": str(proposal.id)},
        )
    )
    session.flush()


def check(session: Session, proposal: Proposal, now: datetime, actor: str) -> str:
    """Apply validity to one proposal, recording an expiry or supersede. Returns it.

    Locks the item first (see `load`), so the version read here stays current.
    """
    lock_item(session, proposal.item_id)
    # Another transaction may have decided it since it was read
    session.refresh(proposal, with_for_update=True)
    current = current_version(session, proposal.item_id)
    state = validity(proposal.status, proposal.base_version, current, proposal.expires_at, now)
    if proposal.status != "pending" or state == VALID:
        return state
    mark(session, proposal, state, actor)
    if state == EXPIRED:
        to_review(session, proposal, actor)
    return state


def sweep(session: Session, now: datetime, actor: str) -> dict[str, int]:
    """Check every pending proposal. Returns how many expired and were superseded."""
    query = select(Proposal).where(Proposal.status == "pending").order_by(Proposal.created_at)
    counts = {EXPIRED: 0, SUPERSEDED: 0}
    for proposal in session.execute(query).scalars().all():
        state = check(session, proposal, now, actor)
        if state in counts:
            counts[state] += 1
    return counts


def load(session: Session, proposal_id: uuid.UUID) -> Proposal:
    """The proposal, locked after its item.

    Item first, then proposal: the same order extraction uses when it
    supersedes older proposals, so the two cannot deadlock.
    """
    find = select(Proposal.item_id).where(Proposal.id == proposal_id)
    item_id = session.execute(find).scalar_one_or_none()
    if item_id is None:
        raise UnknownProposal(str(proposal_id))
    lock_item(session, item_id)
    query = select(Proposal).where(Proposal.id == proposal_id).with_for_update()
    return session.execute(query).scalar_one()


def decidable(session: Session, proposal_id: uuid.UUID, now: datetime) -> Proposal:
    """The proposal, locked with its item, if it can still be decided.

    An expiry or supersede found here is recorded before NotPending is
    raised, so the caller should commit either way.
    """
    proposal = load(session, proposal_id)
    state = check(session, proposal, now, "user")
    if state != VALID:
        raise NotPending(state)
    return proposal


def verify_hash(proposal: Proposal, shown: str) -> None:
    computed = payload_hash(proposal.payload)
    if computed != proposal.payload_hash or shown != proposal.payload_hash:
        raise HashMismatch()


def refuse(session: Session, proposal: Proposal) -> None:
    data = {"status": proposal.status, "reason": "hash_mismatch"}
    session.add(event("refused", proposal, "user", data))


@dataclass(frozen=True)
class Applied:
    proposal_id: uuid.UUID
    item_id: uuid.UUID
    version: int


def apply(session: Session, proposal: Proposal) -> int:
    """Write the change to the item and append its version. Returns the version."""
    item = lock_item(session, proposal.item_id)
    after = proposal.payload["after"]
    number = current_version(session, item.id) + 1
    new_status = "open" if item.status in ACTIVE else item.status
    before = {**proposal.payload["before"], "status": item.status}
    version = ItemVersion(
        item_id=item.id,
        version=number,
        change_type="update",
        actor="user",
        before=before,
        after={
            "due_raw": after["due_raw"],
            "due_at": after["due_at"],
            "status": new_status,
            "review": None,
            "proposal_id": str(proposal.id),
        },
        evidence=proposal.payload.get("evidence"),
        message_id=proposal.message_id,
        extraction_run_id=proposal.extraction_run_id,
    )
    item.due_raw = after["due_raw"]
    item.due_at = parse_utc(after["due_at"])
    item.status = new_status
    session.add(version)
    session.flush()
    return number


def approve(session: Session, proposal_id: uuid.UUID, shown: str, now: datetime) -> Applied:
    """Check, then apply in the caller's transaction. Raises on any refusal."""
    proposal = decidable(session, proposal_id, now)
    try:
        verify_hash(proposal, shown)
    except HashMismatch:
        refuse(session, proposal)
        raise
    number = apply(session, proposal)
    proposal.status = "approved"
    proposal.decided_at = now
    session.add(event("approved", proposal, "user", {"from": "pending", "to": "approved"}))
    session.flush()
    proposal.status = "executed"
    data = {"from": "approved", "to": "executed", "version": number}
    session.add(event("executed", proposal, "user", data))
    session.flush()
    return Applied(proposal.id, proposal.item_id, number)


def reject(session: Session, proposal_id: uuid.UUID, now: datetime) -> None:
    """Reject a pending proposal. The item stays as it is."""
    proposal = decidable(session, proposal_id, now)
    proposal.status = "rejected"
    proposal.decided_at = now
    session.add(event("rejected", proposal, "user", {"from": "pending", "to": "rejected"}))
    session.flush()
