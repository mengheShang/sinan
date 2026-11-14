"""Notices end to end (ADR 0017): extraction, digest, Past view, "Make it a to-do".

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

from app.auth import Auth, LoginLimiter, SessionSigner, hash_password
from app.config import Settings, get_settings
from app.db.digest import latest, list_items, make_digest, past_items, read_digest
from app.db.extract import extract_pending
from app.db.item_kind import NotANotice, make_todo
from app.db.models import (
    Account,
    AuditEvent,
    Digest,
    ExtractionRun,
    Item,
    ItemVersion,
    Message,
    Proposal,
)
from app.extraction.prefilter import Rules
from app.extraction.prompt import PROMPT_HASH, PROMPT_VERSION
from app.providers.fakes import FakeExtractor
from apps.api.auth import COOKIE, get_auth
from apps.api.clock import get_clock
from apps.api.main import app
from apps.api.oauth import get_session

pytestmark = pytest.mark.db

LA = ZoneInfo("America/Los_Angeles")
DAY = timedelta(days=1)
# Wednesday Nov 11 2026, 07:30 in Los Angeles
MORNING = datetime(2026, 11, 11, 15, 30, tzinfo=UTC)
EMAIL = "student@example.edu"
SECRET = "fake-session-secret-for-tests-only-0123456789"

WORKSHOP = "Interested students may register for the resume workshop by Friday Nov 20 at 5pm PT."
ESSAY = "Essay 2 is due Friday Nov 20 at 11:59pm PT."
MOVED = "The resume workshop registration now closes Monday Nov 23 at 5pm PT."
# Friday Nov 20, 17:00 and 23:59 in Los Angeles
WORKSHOP_DUE = datetime(2026, 11, 21, 1, 0, tzinfo=UTC)
ESSAY_DUE = datetime(2026, 11, 21, 7, 59, tzinfo=UTC)


def entry(kind: str, title: str, evidence: str, due_raw: str, change: str = "create") -> str:
    item = {
        "kind": kind,
        "course": None,
        "title": title,
        "due_raw": due_raw,
        "evidence": evidence,
        "change_type": change,
    }
    return json.dumps({"items": [item]})


REPLIES = {
    WORKSHOP: entry("notice", "Resume workshop", WORKSHOP, "Friday Nov 20 at 5pm PT"),
    ESSAY: entry("deadline", "Essay 2", ESSAY, "Friday Nov 20 at 11:59pm PT"),
    MOVED: entry("notice", "Resume workshop", MOVED, "Monday Nov 23 at 5pm PT", "update"),
}


@pytest.fixture
def session(conn: Connection) -> Session:
    return Session(bind=conn, join_transaction_mode="create_savepoint")


def account_id(session: Session) -> uuid.UUID:
    found = session.execute(sa.select(Account.id).where(Account.email == EMAIL)).scalar()
    if found is not None:
        return found
    account = Account(provider="gmail", email=EMAIL, role="school")
    session.add(account)
    session.flush()
    return account.id


def message(session: Session, body: str, when: datetime) -> Message:
    msg = Message(
        account_id=account_id(session),
        provider_msg_id=uuid.uuid4().hex[:16],
        received_at=when,
        sent_at=when,
        from_addr="events@example.edu",
        subject="Fictional announcement",
        headers={},
        label_ids=["INBOX"],
        body_text=body,
    )
    session.add(msg)
    session.flush()
    return msg


def extract(session: Session) -> None:
    extract_pending(session, FakeExtractor(REPLIES), Rules(), cap=50, clock=lambda: MORNING)


def by_title(session: Session, title: str) -> Item:
    query = sa.select(Item).where(Item.title == title)
    return session.execute(query).scalar_one()


def seed(
    session: Session,
    title: str,
    created: datetime,
    kind: str = "notice",
    due_at: datetime | None = None,
) -> uuid.UUID:
    """An item with its source message, run and first version, like extraction makes."""
    msg = message(session, f"{title} for interested students.", created)
    run = ExtractionRun(
        message_id=msg.id,
        prompt_version=PROMPT_VERSION,
        prompt_hash=PROMPT_HASH,
        model="fake-extractor",
        status="ok",
    )
    session.add(run)
    session.flush()
    item = Item(
        kind=kind,
        title=title,
        due_raw="Friday 5pm PT" if due_at else None,
        due_at=due_at,
        status="open",
        created_at=created,
    )
    session.add(item)
    session.flush()
    version = ItemVersion(
        item_id=item.id,
        version=1,
        change_type="create",
        actor="extraction",
        after={"kind": kind, "title": title, "review": None},
        evidence=f"{title} for interested students.",
        message_id=msg.id,
        extraction_run_id=run.id,
    )
    session.add(version)
    session.commit()
    return item.id


def digest_at(session: Session, when: datetime) -> Digest:
    made = make_digest(session, when, LA, 7)
    assert made is not None
    return made


def notice_titles(session: Session, digest: Digest, now: datetime = MORNING) -> list[str]:
    return [v.title for v in read_digest(session, digest, now).notices]


def section_titles(session: Session, now: datetime = MORNING) -> set[str]:
    found = latest(session)
    assert found is not None
    view = read_digest(session, found, now)
    titles = {v.title for items in view.sections.values() for v in items}
    return titles | {v.title for v in view.overdue}


# ---- Extraction ----


def test_optional_event_with_a_date_becomes_a_notice_with_its_date(session: Session) -> None:
    message(session, WORKSHOP, MORNING - DAY)
    session.commit()
    extract(session)
    item = by_title(session, "Resume workshop")
    assert item.kind == "notice"
    assert item.status == "open"
    assert item.due_at == WORKSHOP_DUE
    (first,) = session.execute(sa.select(ItemVersion)).scalars()
    assert first.after["kind"] == "notice"


def test_required_deadline_stays_a_deadline(session: Session) -> None:
    message(session, ESSAY, MORNING - DAY)
    session.commit()
    extract(session)
    item = by_title(session, "Essay 2")
    assert item.kind == "deadline"
    assert item.due_at == ESSAY_DUE


def test_a_notice_update_is_never_matched_or_proposed(session: Session) -> None:
    message(session, WORKSHOP, MORNING - 2 * DAY)
    message(session, MOVED, MORNING - DAY)
    session.commit()
    extract(session)
    assert session.execute(sa.select(Proposal)).first() is None
    items = list(session.execute(sa.select(Item)).scalars())
    assert [(i.kind, i.status) for i in items] == [("notice", "open"), ("notice", "open")]


def test_a_deadline_change_never_targets_a_notice(session: Session) -> None:
    seed(session, "Essay 2", MORNING - 3 * DAY, kind="notice", due_at=ESSAY_DUE)
    body = "Essay 2 is now due Monday Nov 23 at 11:59pm PT."
    message(session, body, MORNING - DAY)
    session.commit()
    change = entry("deadline", "Essay 2", body, "Monday Nov 23 at 11:59pm PT", "update")
    extractor = FakeExtractor({body: change})
    extract_pending(session, extractor, Rules(), cap=50, clock=lambda: MORNING)
    assert session.execute(sa.select(Proposal)).first() is None
    query = sa.select(Item).where(Item.kind == "deadline")
    (saved,) = session.execute(query).scalars()
    assert saved.status == "needs_review"


# ---- Digest ----


def test_notices_never_enter_sections_overdue_or_past(session: Session) -> None:
    seed(session, "Workshop soon", MORNING - DAY, due_at=MORNING + DAY)
    seed(session, "Talk last week", MORNING - 9 * DAY, due_at=MORNING - 7 * DAY)
    seed(session, "Safety alert", MORNING - DAY)
    seed(session, "Homework 3", MORNING - DAY, kind="deadline", due_at=MORNING + DAY)
    made = digest_at(session, MORNING)
    assert section_titles(session) == {"Homework 3"}
    assert sorted(notice_titles(session, made)) == [
        "Safety alert",
        "Talk last week",
        "Workshop soon",
    ]
    later = MORNING + 20 * DAY
    assert section_titles(session, later) == set()
    past = past_items(session, later)
    assert [v.title for v in past.past] == ["Homework 3"]
    assert past.stale == []
    assert {v.title for v in past.notices} == {"Safety alert", "Talk last week", "Workshop soon"}
    assert [v.title for v in list_items(session, ("open", "needs_review"))] == ["Homework 3"]


def test_a_notice_appears_in_exactly_one_digest(session: Session) -> None:
    seed(session, "Flu shots", MORNING - DAY)
    first = digest_at(session, MORNING)
    seed(session, "Storm alert", MORNING + timedelta(hours=3))
    second = digest_at(session, MORNING + DAY)
    third = digest_at(session, MORNING + 2 * DAY)
    assert notice_titles(session, first) == ["Flu shots"]
    assert notice_titles(session, second) == ["Storm alert"]
    assert notice_titles(session, third) == []


def test_notice_count_is_audited(session: Session) -> None:
    seed(session, "Flu shots", MORNING - DAY)
    digest_at(session, MORNING)
    query = sa.select(AuditEvent).where(AuditEvent.event_type == "digest")
    event = session.execute(query).scalar_one()
    assert event.data["notices"] == 1
    assert event.data["new"] == 0


def test_notices_are_oldest_first(session: Session) -> None:
    seed(session, "Second", MORNING - DAY)
    seed(session, "First", MORNING - 2 * DAY)
    made = digest_at(session, MORNING)
    assert notice_titles(session, made) == ["First", "Second"]


# ---- Make it a to-do ----


def test_make_todo_appends_a_user_version(session: Session) -> None:
    item_id = seed(session, "Resume workshop", MORNING - DAY, due_at=WORKSHOP_DUE)
    version = make_todo(session, item_id)
    session.commit()
    assert version.version == 2
    assert version.actor == "user"
    assert version.change_type == "update"
    assert version.before == {"kind": "notice"}
    assert version.after == {"kind": "todo"}
    item = session.get_one(Item, item_id)
    assert (item.kind, item.status, item.due_at) == ("todo", "open", WORKSHOP_DUE)
    query = sa.select(AuditEvent).where(AuditEvent.event_type == "item.kind")
    event = session.execute(query).scalar_one()
    assert event.actor == "user"
    assert event.data == {"from": "notice", "to": "todo", "version": 2}


def test_make_todo_twice_is_refused(session: Session) -> None:
    item_id = seed(session, "Resume workshop", MORNING - DAY)
    make_todo(session, item_id)
    session.commit()
    with pytest.raises(NotANotice):
        make_todo(session, item_id)
    deadline = seed(session, "Homework 3", MORNING - DAY, kind="deadline", due_at=ESSAY_DUE)
    with pytest.raises(NotANotice):
        make_todo(session, deadline)


def test_a_made_todo_stays_in_its_digest_and_joins_the_lists(session: Session) -> None:
    item_id = seed(session, "Resume workshop", MORNING - DAY, due_at=MORNING + 2 * DAY)
    made = digest_at(session, MORNING)
    make_todo(session, item_id)
    session.commit()
    (view,) = read_digest(session, made, MORNING).notices
    assert view.kind == "todo"
    assert [v.title for v in list_items(session, ("open",))] == ["Resume workshop"]
    assert past_items(session, MORNING).notices == []
    tomorrow = digest_at(session, MORNING + DAY)
    due_soon = read_digest(session, tomorrow, MORNING + DAY).sections["due_soon"]
    assert [v.title for v in due_soon] == ["Resume workshop"]
    assert notice_titles(session, tomorrow, MORNING + DAY) == []


# ---- API ----


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    password_hash = hash_password("unused", n=2**4)
    signer = SessionSigner(SECRET, "owner", password_hash)
    auth = Auth("owner", password_hash, signer, LoginLimiter())
    settings = Settings(sinan_mode="demo", sinan_timezone="America/Los_Angeles")
    settings = settings.model_copy(update={"sinan_mode": "live"})
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_auth] = lambda: auth
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_clock] = lambda: lambda: MORNING
    with TestClient(app) as c:
        c.cookies.set(COOKIE, signer.issue(auth.clock()))
        yield c
    app.dependency_overrides.clear()


def test_api_digest_lists_notices_with_date_and_link(client: TestClient, session: Session) -> None:
    seed(session, "Resume workshop", MORNING - DAY, due_at=WORKSHOP_DUE)
    digest_at(session, MORNING)
    body = client.get("/digest").json()
    (notice,) = body["notices"]
    assert notice["kind"] == "notice"
    assert notice["due_at"] == "2026-11-21T01:00:00Z"
    assert notice["gmail_url"].startswith("https://mail.google.com/mail/?authuser=")
    assert body["new"] == [] and body["due_soon"] == []
    assert client.get("/items").json() == []
    past = client.get("/items/past").json()
    assert [n["title"] for n in past["notices"]] == ["Resume workshop"]


def test_api_make_todo(client: TestClient, session: Session) -> None:
    item_id = seed(session, "Resume workshop", MORNING - DAY)
    url = f"/items/{item_id}/kind"
    resp = client.post(url, json={"kind": "todo"})
    assert resp.status_code == 200
    assert resp.json() == {"kind": "todo", "version": 2}
    assert client.post(url, json={"kind": "todo"}).status_code == 409
    assert client.post(url, json={"kind": "deadline"}).status_code == 422
    assert client.post(url, json={"kind": "notice"}).status_code == 422
    assert client.post(f"/items/{uuid.uuid4()}/kind", json={"kind": "todo"}).status_code == 404


def test_api_make_todo_needs_login(client: TestClient, session: Session) -> None:
    item_id = seed(session, "Resume workshop", MORNING - DAY)
    client.cookies.clear()
    assert client.post(f"/items/{item_id}/kind", json={"kind": "todo"}).status_code == 401
