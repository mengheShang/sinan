"""Digest and item endpoints for the Dashboard.

All sit behind the login like every other route. The Worker makes digests;
the only writes here are the owner changing an item's status or making a
notice a to-do (ADR 0017), which touch the local database alone.
Proposed changes are in apps/api/proposals.py.
"""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.db.digest import ItemView, latest, list_items, past_items, read_digest
from app.db.item_kind import NotANotice, make_todo
from app.db.item_status import BadMove, UnknownItem, set_status
from apps.api.clock import ClockDep
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
    time_carried: bool


class DigestOut(BaseModel):
    digest_date: date
    generated_at: datetime
    since: datetime | None
    # IANA zone for showing times; all times above are UTC
    timezone: str
    # Computed at read time from all active items, never stored (ADR 0015)
    overdue: list[ItemOut]
    new: list[ItemOut]
    due_soon: list[ItemOut]
    needs_review: list[ItemOut]
    # Proposed changes that expired unanswered since the previous digest
    expired_proposals: int
    # Found as notices since the previous digest, in their current state;
    # computed at read time, never stored (ADR 0017)
    notices: list[ItemOut]


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
        time_carried=view.assumed.carried,
    )


@router.get("/digest")
def digest(session: SessionDep, settings: SettingsDep, clock: ClockDep) -> DigestOut | None:
    """The latest digest as it stands now, or null before the Worker has made one.

    Stored items that are now overdue, past or stale leave their sections.
    """
    found = latest(session)
    if found is None:
        return None
    view = read_digest(session, found, clock())
    live = settings.sinan_mode == "live"
    sections = {name: [item_out(v, live) for v in items] for name, items in view.sections.items()}
    return DigestOut(
        digest_date=view.digest_date,
        generated_at=view.generated_at,
        since=view.since,
        timezone=settings.sinan_timezone,
        overdue=[item_out(v, live) for v in view.overdue],
        new=sections["new"],
        due_soon=sections["due_soon"],
        needs_review=sections["needs_review"],
        expired_proposals=view.expired_proposals,
        notices=[item_out(v, live) for v in view.notices],
    )


@router.get("/items")
def items(
    session: SessionDep,
    settings: SettingsDep,
    status: Annotated[list[Status] | None, Query()] = None,
) -> list[ItemOut]:
    """Deadlines and to-dos with the given statuses; open and needs_review by
    default. Notices are never listed here."""
    wanted = tuple(status) if status else ("open", "needs_review")
    live = settings.sinan_mode == "live"
    return [item_out(v, live) for v in list_items(session, wanted)]


class PastOut(BaseModel):
    # Due two or more days ago, most recently due first
    past: list[ItemOut]
    # Needs review, no due time, source email over 14 days old
    stale: list[ItemOut]
    # Every open notice, most recently received first
    notices: list[ItemOut]


@router.get("/items/past")
def past(session: SessionDep, settings: SettingsDep, clock: ClockDep) -> PastOut:
    """Open and needs_review items that have left the digest, still open to act
    on, and every open notice."""
    view = past_items(session, clock())
    live = settings.sinan_mode == "live"
    return PastOut(
        past=[item_out(v, live) for v in view.past],
        stale=[item_out(v, live) for v in view.stale],
        notices=[item_out(v, live) for v in view.notices],
    )


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


class KindBody(BaseModel):
    kind: Literal["todo"]


class KindOut(BaseModel):
    kind: str
    version: int


@router.post("/items/{item_id}/kind")
def change_kind(item_id: uuid.UUID, body: KindBody, session: SessionDep) -> KindOut:
    """Make a notice a to-do. 409 when the item is not a notice, e.g. a second click."""
    try:
        version = make_todo(session, item_id)
    except UnknownItem as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown item") from exc
    except NotANotice as exc:
        session.rollback()
        detail = f"Only a notice can be made a to-do, not a {exc.kind}"
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from exc
    session.commit()
    return KindOut(kind=body.kind, version=version.version)
