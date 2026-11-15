"""Proposal endpoints for the Dashboard: list, approve and reject.

All sit behind the login like every other route. Listing writes nothing:
it computes each proposal's status now (expiry and superseding, ADR 0009
decision 6, ADR 0014) with the same function that approving and rejecting
use; those record what they find in their own transaction, and the Worker's
sweep records the rest. Approving writes only the local database; nothing
external is touched.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.actions.proposals import (
    HashMismatch,
    NotPending,
    UnknownProposal,
    approve,
    effective,
    reject,
)
from app.db.models import Account, Item, Message, Proposal
from app.items.assumed import assumed_of
from app.proposals import HASH_PATTERN, Link, link_of
from apps.api.clock import ClockDep
from apps.api.digest import SettingsDep
from apps.api.oauth import SessionDep

router = APIRouter()

Status = Literal["pending", "approved", "rejected", "expired", "superseded", "executed"]


class DueOut(BaseModel):
    due_raw: str | None
    due_at: datetime | None
    # Parts of due_at that were assumed, not written (ADR 0016)
    zone_assumed: bool
    time_assumed: bool
    date_computed: bool
    time_carried: bool


class ProposalOut(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    item_id: uuid.UUID
    course: str | None
    title: str
    # The due time when the proposal was made, and the proposed one
    before: DueOut
    after: DueOut
    evidence: str | None
    # How the item was chosen (ADR 0019); "llm" gets a badge on the Dashboard.
    # Part of the hashed payload; older payloads without it read as deterministic
    link: Link
    # Sent back on approval, so the owner approves exactly what was shown
    payload_hash: str
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    subject: str | None
    sender: str | None
    received_at: datetime | None
    gmail_url: str | None


def due_out(part: Any) -> DueOut:
    data = part if isinstance(part, dict) else {}
    flags = assumed_of(data)
    return DueOut(
        due_raw=data.get("due_raw"),
        due_at=data.get("due_at"),
        zone_assumed=flags.zone,
        time_assumed=flags.time,
        date_computed=flags.date,
        time_carried=flags.carried,
    )


def gmail_url(msg: Message, email: str, live: bool) -> str | None:
    if not live:
        return None
    return f"https://mail.google.com/mail/?authuser={email}#all/{msg.provider_msg_id}"


def to_out(row: Any, state: str, live: bool) -> ProposalOut:
    proposal: Proposal = row[0]
    item: Item = row[1]
    msg: Message = row[2]
    email: str = row[3]
    payload = proposal.payload
    evidence = payload.get("evidence")
    return ProposalOut(
        id=proposal.id,
        kind=proposal.kind,
        # Computed now, so an unswept proposal past its expiry reads as expired
        status=state,
        item_id=item.id,
        course=item.course,
        title=item.title,
        before=due_out(payload.get("before")),
        after=due_out(payload.get("after")),
        evidence=evidence if isinstance(evidence, str) else None,
        link=link_of(payload),
        payload_hash=proposal.payload_hash,
        created_at=proposal.created_at,
        expires_at=proposal.expires_at,
        decided_at=proposal.decided_at,
        subject=msg.subject,
        sender=msg.from_addr,
        received_at=msg.received_at,
        gmail_url=gmail_url(msg, email, live),
    )


def rows(session: Session, stored: set[str]) -> list[Any]:
    query = (
        select(Proposal, Item, Message, Account.email)
        .join(Item, Item.id == Proposal.item_id)
        .join(Message, Message.id == Proposal.message_id)
        .join(Account, Account.id == Message.account_id)
        .where(Proposal.status.in_(stored))
        .order_by(Proposal.expires_at, Proposal.id)
    )
    return list(session.execute(query).all())


@router.get("/proposals")
def proposals(
    session: SessionDep,
    settings: SettingsDep,
    clock: ClockDep,
    status: Annotated[list[Status] | None, Query()] = None,
) -> list[ProposalOut]:
    """Proposals whose status now is one of these, pending by default.

    Soonest expiry first. Read only: a stored `pending` proposal past its
    expiry, or whose item has changed, is shown as expired or superseded
    without being recorded as such.
    """
    wanted = set(status) if status else {"pending"}
    # Any still-pending row may now be expired or superseded
    stored = wanted | {"pending"}
    now = clock()
    live = settings.sinan_mode == "live"
    found = []
    for row in rows(session, stored):
        state = effective(session, row[0], now)
        if state in wanted:
            found.append(to_out(row, state, live))
    return found


class ApproveBody(BaseModel):
    payload_hash: str = Field(pattern=HASH_PATTERN)


class DecisionOut(BaseModel):
    status: str
    # The item version the change created; None for a rejection
    version: int | None = None


def not_pending(exc: NotPending) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, f"Proposal is {exc.status}")


@router.post("/proposals/{proposal_id}/approve")
def approve_one(
    proposal_id: uuid.UUID, body: ApproveBody, session: SessionDep, clock: ClockDep
) -> DecisionOut:
    """Apply the change to the item. 409 when it is no longer pending or changed."""
    try:
        applied = approve(session, proposal_id, body.payload_hash, clock())
    except UnknownProposal as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown proposal") from exc
    except NotPending as exc:
        # Keep the expiry or supersede that the check recorded
        session.commit()
        raise not_pending(exc) from exc
    except HashMismatch as exc:
        # Keep the refusal's audit event; nothing was applied
        session.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, "Proposal does not match") from exc
    session.commit()
    return DecisionOut(status="executed", version=applied.version)


@router.post("/proposals/{proposal_id}/reject")
def reject_one(proposal_id: uuid.UUID, session: SessionDep, clock: ClockDep) -> DecisionOut:
    """Reject the change; the item stays as it is. 409 when no longer pending."""
    try:
        reject(session, proposal_id, clock())
    except UnknownProposal as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown proposal") from exc
    except NotPending as exc:
        session.commit()
        raise not_pending(exc) from exc
    session.commit()
    return DecisionOut(status="rejected")
