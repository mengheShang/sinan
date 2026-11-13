"""Digest and item endpoints for the Dashboard.

All sit behind the login like every other route. The Worker makes digests;
the only write here is the owner changing an item's status, which touches
the local database alone. Proposed changes are in apps/api/proposals.py.
"""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.db.digest import ItemView, latest, list_items, read_digest
from app.db.item_status import BadMove, UnknownItem, set_status
from apps.api.oauth import SessionDep

router = APIRouter()

SettingsDep = Annotated[Settings, Depends(get_settings)]
Status = Literal["open", "needs_review", "done", "dismissed"]


class ItemOut(BaseModel):
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
    # Opens the source email in Gmail; None in demo mode, whose mail is fake
    gmail_url: str | None
    # Parts of due_at that were assumed, not written (ADR 0016)
    zone_assumed: bool
    time_assumed: bool
    date_computed: bool


class DigestOut(BaseModel):
    digest_date: date
    generated_at: datetime
    since: datetime | None
    # IANA zone for showing times; all times above are UTC
    timezone: str
    new: list[ItemOut]
    due_soon: list[ItemOut]
    needs_review: list[ItemOut]
    # Proposed changes that expired unanswered since the previous digest
    expired_proposals: int


def gmail_url(view: ItemView, live: bool) -> str | None:
    """A link to the message in Gmail's web app, signed in as its mailbox."""
    if not live or view.account_email is None or view.provider_msg_id is None:
        return None
    account = quote(view.account_email, safe="@")
    return f"https://mail.google.com/mail/?authuser={account}#all/{view.provider_msg_id}"


def item_out(view: ItemView, live: bool) -> ItemOut:
    return ItemOut(
        id=view.id,
        kind=view.kind,
        course=view.course,
        title=view.title,
        due_raw=view.due_raw,
        due_at=view.due_at,
        status=view.status,
        review=view.review,
        evidence=view.evidence,
        created_at=view.created_at,
        subject=view.subject,
        sender=view.sender,
        received_at=view.received_at,
        gmail_url=gmail_url(view, live),
        zone_assumed=view.assumed.zone,
        time_assumed=view.assumed.time,
        date_computed=view.assumed.date,
    )


@router.get("/digest")
def digest(session: SessionDep, settings: SettingsDep) -> DigestOut | None:
    """The latest digest, or null before the Worker has made one."""
    found = latest(session)
    if found is None:
        return None
    view = read_digest(session, found)
    live = settings.sinan_mode == "live"
    sections = {name: [item_out(v, live) for v in items] for name, items in view.sections.items()}
    return DigestOut(
        digest_date=view.digest_date,
        generated_at=view.generated_at,
        since=view.since,
        timezone=settings.sinan_timezone,
        new=sections["new"],
        due_soon=sections["due_soon"],
        needs_review=sections["needs_review"],
        expired_proposals=view.expired_proposals,
    )


@router.get("/items")
def items(
    session: SessionDep,
    settings: SettingsDep,
    status: Annotated[list[Status] | None, Query()] = None,
) -> list[ItemOut]:
    """Items with the given statuses; open and needs_review by default."""
    wanted = tuple(status) if status else ("open", "needs_review")
    live = settings.sinan_mode == "live"
    return [item_out(v, live) for v in list_items(session, wanted)]


class StatusBody(BaseModel):
    status: Literal["open", "done", "dismissed"]


class StatusOut(BaseModel):
    status: str
    version: int


@router.post("/items/{item_id}/status")
def change_status(item_id: uuid.UUID, body: StatusBody, session: SessionDep) -> StatusOut:
    """Mark an item done or dismissed, or reopen it. 409 for a move not allowed."""
    try:
        version = set_status(session, item_id, body.status)
    except UnknownItem as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown item") from exc
    except BadMove as exc:
        session.rollback()
        detail = f"Cannot move from {exc.old} to {exc.new}"
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from exc
    session.commit()
    return StatusOut(status=body.status, version=version.version)
