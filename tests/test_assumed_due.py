"""Assumed zones and times end to end: storage, the API and proposals (ADR 0016).

Synthetic mail and a FakeExtractor only. Every course, person and address is
fictional.
"""

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.actions.proposals import approve
from app.auth import Auth, LoginLimiter, SessionSigner, hash_password
from app.config import Settings, get_settings
from app.db.digest import make_digest
from app.db.extract import extract_pending
from app.db.models import Account, Item, ItemVersion, Message, Proposal
from app.extraction.prefilter import Rules
from app.proposals import payload_hash
from app.providers.fakes import FakeExtractor
from apps.api.auth import COOKIE, get_auth
from apps.api.clock import get_clock
from apps.api.main import app
from apps.api.oauth import get_session

pytestmark = pytest.mark.db

# Monday Nov 2 2026, 09:00 in Los Angeles; extraction runs at RUN
T0 = datetime(2026, 11, 2, 17, 0, tzinfo=UTC)
RUN = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
SECRET = "fake-session-secret-for-tests-only-0123456789"
# The API's clock; a test may move it
CLOCK = [RUN]

DATE_ONLY = "Homework 3 is due Friday Nov 13."
NO_ZONE = "Homework 3 is now due Monday Nov 16 at 5pm."
STATED = "Homework 3 is now due Tuesday Nov 17 at 11:59pm PT."
COUNTED = "Homework 3 is due in 3 days."
POSTED_5PM = "Homework 3 is due Friday Nov 13 at 5pm PT."
MOVED_DATE = "Homework 3 is now due Nov 20."
SAME_DAY = "Homework 3 is still due Nov 13."


def entry(evidence: str, due_raw: str, change: str) -> str:
    item = {
        "kind": "deadline",
        "course": "CS 000",
        "title": "Homework 3",
        "due_raw": due_raw,
        "evidence": evidence.rstrip("."),
        "change_type": change,
    }
    return json.dumps({"items": [item]})


REPLIES = {
    DATE_ONLY: entry(DATE_ONLY, "Friday Nov 13", "create"),
    NO_ZONE: entry(NO_ZONE, "Monday Nov 16 at 5pm", "update"),
    STATED: entry(STATED, "Tuesday Nov 17 at 11:59pm PT", "update"),
    COUNTED: entry(COUNTED, "in 3 days", "create"),
    POSTED_5PM: entry(POSTED_5PM, "Friday Nov 13 at 5pm PT", "create"),
    MOVED_DATE: entry(MOVED_DATE, "Nov 20", "update"),
    SAME_DAY: entry(SAME_DAY, "Nov 13", "update"),
}


@pytest.fixture
def session(conn: Connection) -> Session:
    return Session(bind=conn, join_transaction_mode="create_savepoint")


def account_id(session: Session) -> uuid.UUID:
    email = "student@example.edu"
    found = session.execute(sa.select(Account.id).where(Account.email == email)).scalar()
    if found is not None:
        return found
    account = Account(provider="gmail", email=email, role="school")
    session.add(account)
    session.flush()
    return account.id


def mail(session: Session, body: str, hours: int) -> None:
    """A fictional email dated `hours` after T0."""
    when = T0 + timedelta(hours=hours)
    msg = Message(
        account_id=account_id(session),
        provider_msg_id=uuid.uuid4().hex,
        received_at=when,
        sent_at=when,
        from_addr="rivera@example.edu",
        subject="CS 000: Homework 3",
        headers={},
        label_ids=["INBOX"],
        body_text=body,
    )
    session.add(msg)
    session.commit()


def extract(session: Session) -> None:
    fake = FakeExtractor(REPLIES)
    extract_pending(session, fake, Rules(), cap=50, ttl_days=7, clock=lambda: RUN)


def only_item(session: Session) -> Item:
    return session.execute(sa.select(Item)).scalar_one()


def only_proposal(session: Session) -> Proposal:
    return session.execute(sa.select(Proposal)).scalar_one()


def newest_after(session: Session, item_id: uuid.UUID) -> dict[str, object]:
    query = (
        sa.select(ItemVersion.after)
        .where(ItemVersion.item_id == item_id)
        .order_by(ItemVersion.version.desc())
        .limit(1)
    )
    after: dict[str, object] = session.execute(query).scalar_one()
    return after


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    password_hash = hash_password("unused", n=2**4)
    signer = SessionSigner(SECRET, "owner", password_hash)
    auth = Auth("owner", password_hash, signer, LoginLimiter())
    settings = Settings(sinan_mode="demo", sinan_timezone="America/Los_Angeles")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_auth] = lambda: auth
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_clock] = lambda: lambda: CLOCK[0]
    with TestClient(app) as c:
        c.cookies.set(COOKIE, signer.issue(auth.clock()))
        yield c
    app.dependency_overrides.clear()
    CLOCK[0] = RUN


def test_extraction_stores_the_assumed_parts(session: Session) -> None:
    mail(session, DATE_ONLY, 0)
    extract(session)
    item = only_item(session)
    assert item.status == "open"
    assert item.due_at == datetime(2026, 11, 14, 7, 59, tzinfo=UTC)
    after = newest_after(session, item.id)
    assert after["zone_assumed"] is True
    assert after["time_assumed"] is True


def test_extraction_uses_the_given_zone(session: Session) -> None:
    mail(session, DATE_ONLY, 0)
    fake = FakeExtractor(REPLIES)
    eastern = ZoneInfo("America/New_York")
    extract_pending(session, fake, Rules(), cap=50, clock=lambda: RUN, home=eastern)
    assert only_item(session).due_at == datetime(2026, 11, 14, 4, 59, tzinfo=UTC)


def test_api_shows_the_assumed_parts(client: TestClient, session: Session) -> None:
    mail(session, DATE_ONLY, 0)
    extract(session)
    (shown,) = client.get("/items").json()
    assert shown["zone_assumed"] is True
    assert shown["time_assumed"] is True
    make_digest(session, RUN, ZoneInfo("America/Los_Angeles"), 7, force=True)
    (new,) = client.get("/digest").json()["new"]
    assert new["zone_assumed"] is True
    assert new["time_assumed"] is True


def test_items_without_flags_read_as_written(client: TestClient, session: Session) -> None:
    # Versions saved before ADR 0016 have no flags
    due = datetime(2026, 11, 14, 7, 59, tzinfo=UTC)
    item = Item(kind="deadline", title="Homework 3", due_raw="Nov 13", due_at=due, status="open")
    session.add(item)
    session.flush()
    after = {"due_raw": "Nov 13", "due_at": due.isoformat(), "status": "open", "review": None}
    version = ItemVersion(
        item_id=item.id, version=1, change_type="create", actor="user", after=after
    )
    session.add(version)
    session.commit()
    (shown,) = client.get("/items").json()
    assert shown["zone_assumed"] is False
    assert shown["time_assumed"] is False


def test_proposal_carries_both_sides_flags(client: TestClient, session: Session) -> None:
    mail(session, DATE_ONLY, 0)
    mail(session, NO_ZONE, 24)
    extract(session)
    payload = only_proposal(session).payload
    assert payload["before"]["zone_assumed"] is True
    assert payload["before"]["time_assumed"] is True
    assert payload["after"]["zone_assumed"] is True
    assert payload["after"]["time_assumed"] is False
    (shown,) = client.get("/proposals").json()
    assert shown["before"]["time_assumed"] is True
    assert shown["after"]["zone_assumed"] is True
    assert shown["after"]["time_assumed"] is False


def test_approval_moves_the_flags_to_the_item(client: TestClient, session: Session) -> None:
    mail(session, DATE_ONLY, 0)
    mail(session, STATED, 24)
    extract(session)
    proposal = only_proposal(session)
    approve(session, proposal.id, proposal.payload_hash, RUN)
    session.commit()
    item = only_item(session)
    after = newest_after(session, item.id)
    assert after["zone_assumed"] is False
    assert after["time_assumed"] is False
    (shown,) = client.get("/items").json()
    assert shown["zone_assumed"] is False
    assert shown["time_assumed"] is False


def test_status_change_keeps_the_flags(client: TestClient, session: Session) -> None:
    mail(session, DATE_ONLY, 0)
    extract(session)
    item = only_item(session)
    resp = client.post(f"/items/{item.id}/status", json={"status": "done"})
    assert resp.status_code == 200
    (shown,) = client.get("/items?status=done").json()
    assert shown["zone_assumed"] is True
    assert shown["time_assumed"] is True


def test_counted_date_is_marked_computed(client: TestClient, session: Session) -> None:
    mail(session, COUNTED, 0)
    extract(session)
    item = only_item(session)
    # Monday Nov 2 plus 3 days, 23:59 in Los Angeles
    assert item.due_at == datetime(2026, 11, 6, 7, 59, tzinfo=UTC)
    assert newest_after(session, item.id)["date_computed"] is True
    (shown,) = client.get("/items").json()
    assert shown["date_computed"] is True
    assert shown["zone_assumed"] is True
    assert shown["time_assumed"] is True


def test_overdue_and_past_show_the_flags(client: TestClient, session: Session) -> None:
    # Due Friday Nov 13, 23:59 in Los Angeles: 07:59 UTC Nov 14
    mail(session, DATE_ONLY, 0)
    extract(session)
    make_digest(session, RUN, ZoneInfo("America/Los_Angeles"), 7, force=True)
    CLOCK[0] = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    (overdue,) = client.get("/digest").json()["overdue"]
    assert overdue["zone_assumed"] is True
    assert overdue["time_assumed"] is True
    CLOCK[0] = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    (past,) = client.get("/items/past").json()["past"]
    assert past["zone_assumed"] is True
    assert past["time_assumed"] is True


# ---- ADR 0016, decision 7: a date-only change keeps the earlier time ----


def test_date_only_change_keeps_the_time_of_day(client: TestClient, session: Session) -> None:
    mail(session, POSTED_5PM, 0)
    mail(session, MOVED_DATE, 24)
    extract(session)
    proposal = only_proposal(session)
    after = proposal.payload["after"]
    # Nov 20 at 5pm PST, not 23:59
    assert after["due_at"] == "2026-11-21T01:00:00Z"
    assert after["time_carried"] is True
    assert after["time_assumed"] is False
    assert after["zone_assumed"] is False
    assert proposal.payload["before"]["time_carried"] is False
    # The flag is part of the hashed payload
    assert payload_hash(proposal.payload) == proposal.payload_hash
    flipped = {**proposal.payload, "after": {**after, "time_carried": False}}
    assert payload_hash(flipped) != proposal.payload_hash
    (shown,) = client.get("/proposals").json()
    assert shown["after"]["time_carried"] is True
    assert shown["after"]["time_assumed"] is False


def test_approved_carried_time_shows_on_the_item(client: TestClient, session: Session) -> None:
    mail(session, POSTED_5PM, 0)
    mail(session, MOVED_DATE, 24)
    extract(session)
    proposal = only_proposal(session)
    approve(session, proposal.id, proposal.payload_hash, RUN)
    session.commit()
    item = only_item(session)
    assert item.due_at == datetime(2026, 11, 21, 1, 0, tzinfo=UTC)
    assert newest_after(session, item.id)["time_carried"] is True
    (shown,) = client.get("/items").json()
    assert shown["time_carried"] is True
    assert shown["time_assumed"] is False


def test_date_only_reminder_of_the_same_day_proposes_nothing(session: Session) -> None:
    mail(session, POSTED_5PM, 0)
    mail(session, SAME_DAY, 24)
    extract(session)
    assert session.execute(sa.select(Proposal)).first() is None
    assert only_item(session).due_at == datetime(2026, 11, 14, 1, 0, tzinfo=UTC)


def test_earlier_assumed_time_is_not_carried(session: Session) -> None:
    mail(session, DATE_ONLY, 0)
    mail(session, MOVED_DATE, 24)
    extract(session)
    after = only_proposal(session).payload["after"]
    assert after["due_at"] == "2026-11-21T07:59:00Z"
    assert after["time_assumed"] is True
    assert after["time_carried"] is False
