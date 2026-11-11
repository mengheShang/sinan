"""Digest making, reading and the read-only API, on seeded synthetic data."""

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
from app.db.digest import latest, make_digest, read_digest
from app.db.models import (
    Account,
    AuditEvent,
    Digest,
    DigestItem,
    ExtractionRun,
    Item,
    ItemVersion,
    Message,
)
from app.extraction.prompt import PROMPT_HASH, PROMPT_VERSION
from apps.api.auth import COOKIE, get_auth
from apps.api.main import app
from apps.api.oauth import get_session
from apps.worker.digest_job import digest_today

pytestmark = pytest.mark.db

LA = ZoneInfo("America/Los_Angeles")
# Wednesday Nov 11 2026, 07:30 in Los Angeles
MORNING = datetime(2026, 11, 11, 15, 30, tzinfo=UTC)
# Same day, 06:30 in Los Angeles: before the digest hour
EARLY = datetime(2026, 11, 11, 14, 30, tzinfo=UTC)
SECRET = "fake-session-secret-for-tests-only-0123456789"
EMAIL = "student@example.edu"


@pytest.fixture
def session(conn: Connection) -> Session:
    return Session(bind=conn, join_transaction_mode="create_savepoint")


def seed_item(
    session: Session,
    title: str,
    created: datetime,
    due_at: datetime | None = None,
    status: str = "open",
    review: str | None = None,
) -> uuid.UUID:
    """An item with its source message, run and first version, like extraction makes."""
    account_id = session.execute(sa.select(Account.id).where(Account.email == EMAIL)).scalar()
    if account_id is None:
        account = Account(provider="gmail", email=EMAIL, role="school")
        session.add(account)
        session.flush()
        account_id = account.id
    msg = Message(
        account_id=account_id,
        provider_msg_id=uuid.uuid4().hex[:16],
        received_at=created,
        sent_at=created,
        from_addr="rivera@example.edu",
        subject=f"CS 000: {title}",
        headers={},
        label_ids=["INBOX"],
        body_text=f"{title} is due soon.",
    )
    session.add(msg)
    session.flush()
    run = ExtractionRun(
        message_id=msg.id,
        prompt_version=PROMPT_VERSION,
        prompt_hash=PROMPT_HASH,
        model="fake-extractor",
        status="ok",
    )
    session.add(run)
    session.flush()
    due_raw = "Friday 11:59pm PT" if due_at else None
    item = Item(
        kind="deadline" if due_at else "todo",
        course="CS 000",
        title=title,
        due_raw=due_raw,
        due_at=due_at,
        status=status,
        created_at=created,
    )
    session.add(item)
    session.flush()
    version = ItemVersion(
        item_id=item.id,
        version=1,
        change_type="create",
        actor="extraction",
        after={"title": title, "review": review},
        evidence=f"{title} is due soon.",
        message_id=msg.id,
        extraction_run_id=run.id,
    )
    session.add(version)
    session.commit()
    return item.id


def sections(session: Session) -> dict[str, list[str]]:
    found = latest(session)
    assert found is not None
    view = read_digest(session, found)
    return {name: [i.title for i in items] for name, items in view.sections.items()}


# ---- Making a digest ----


def test_nothing_before_the_digest_hour(session: Session) -> None:
    assert make_digest(session, EARLY, LA, 7) is None
    assert latest(session) is None


def test_demo_force_ignores_the_hour(session: Session) -> None:
    made = make_digest(session, EARLY, LA, 7, force=True)
    assert made is not None
    assert made.digest_date.isoformat() == "2026-11-11"


def test_sections(session: Session) -> None:
    day = timedelta(days=1)
    seed_item(session, "Soon", MORNING - day, due_at=MORNING + 2 * day)
    seed_item(session, "Later", MORNING - day, due_at=MORNING + 8 * day)
    seed_item(session, "Past", MORNING - day, due_at=MORNING - day)
    seed_item(session, "Check", MORNING - day, status="needs_review", review="numeric_date")
    seed_item(session, "Finished", MORNING - day, due_at=MORNING + day, status="done")
    make_digest(session, MORNING, LA, 7)
    assert sections(session) == {
        # The first digest counts every active item as new
        "new": ["Past", "Soon", "Later", "Check"],
        "due_soon": ["Soon"],
        "needs_review": ["Check"],
    }


def test_new_means_since_the_previous_digest(session: Session) -> None:
    seed_item(session, "Old", MORNING - timedelta(days=2))
    make_digest(session, MORNING - timedelta(days=1), LA, 7)
    seed_item(session, "Fresh", MORNING - timedelta(hours=3))
    made = make_digest(session, MORNING, LA, 7)
    assert made is not None
    assert made.since == MORNING - timedelta(days=1)
    assert sections(session)["new"] == ["Fresh"]


def test_one_digest_per_local_day(session: Session) -> None:
    assert make_digest(session, MORNING, LA, 7) is not None
    later = MORNING + timedelta(hours=10)
    assert make_digest(session, later, LA, 7) is None
    count = session.execute(sa.select(sa.func.count()).select_from(Digest)).scalar_one()
    assert count == 1


def test_missed_morning_is_caught_up(session: Session) -> None:
    afternoon = MORNING + timedelta(hours=7)
    made = make_digest(session, afternoon, LA, 7)
    assert made is not None
    assert made.generated_at == afternoon


def test_local_day_differs_from_utc_day(session: Session) -> None:
    # 23:30 on Nov 11 in Los Angeles is already Nov 12 in UTC
    late = datetime(2026, 11, 12, 7, 30, tzinfo=UTC)
    made = make_digest(session, late, LA, 7)
    assert made is not None
    assert made.digest_date.isoformat() == "2026-11-11"


def test_digest_is_audited_with_counts_only(session: Session) -> None:
    seed_item(session, "Soon", MORNING, due_at=MORNING + timedelta(days=1))
    make_digest(session, MORNING, LA, 7)
    event = session.execute(sa.select(AuditEvent)).scalar_one()
    assert event.event_type == "digest"
    assert event.data == {"date": "2026-11-11", "new": 1, "due_soon": 1, "needs_review": 0}


def test_digest_shows_current_item_state(session: Session) -> None:
    item_id = seed_item(session, "Soon", MORNING, due_at=MORNING + timedelta(days=1))
    make_digest(session, MORNING, LA, 7)
    session.execute(sa.update(Item).where(Item.id == item_id).values(title="Renamed"))
    session.commit()
    assert sections(session)["due_soon"] == ["Renamed"]


# ---- Schema ----


def test_section_is_checked(session: Session) -> None:
    item_id = seed_item(session, "Soon", MORNING)
    made = make_digest(session, MORNING, LA, 7)
    assert made is not None
    bad = sa.insert(DigestItem).values(digest_id=made.id, item_id=item_id, section="later")
    with pytest.raises(sa.exc.IntegrityError):
        session.execute(bad)
    session.rollback()


def test_item_in_a_digest_cannot_be_deleted(session: Session) -> None:
    # An item without versions, so only the digest link can block the delete
    item = Item(kind="todo", title="Bare", status="open")
    session.add(item)
    session.flush()
    made = make_digest(session, MORNING, LA, 7)
    assert made is not None
    with pytest.raises(sa.exc.IntegrityError):
        session.execute(sa.delete(Item).where(Item.id == item.id))
    session.rollback()


def test_deleting_a_digest_removes_its_links(session: Session) -> None:
    seed_item(session, "Soon", MORNING)
    made = make_digest(session, MORNING, LA, 7)
    assert made is not None
    session.execute(sa.delete(Digest).where(Digest.id == made.id))
    session.commit()
    count = session.execute(sa.select(sa.func.count()).select_from(DigestItem)).scalar_one()
    assert count == 0


# ---- API ----


def api_client(session: Session, mode: str) -> Iterator[TestClient]:
    password_hash = hash_password("unused", n=2**4)
    signer = SessionSigner(SECRET, "owner", password_hash)
    auth = Auth("owner", password_hash, signer, LoginLimiter())
    settings = Settings(sinan_mode="demo", sinan_timezone="America/Los_Angeles")
    settings = settings.model_copy(update={"sinan_mode": mode})
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_auth] = lambda: auth
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        c.cookies.set(COOKIE, signer.issue(auth.clock()))
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    yield from api_client(session, "live")


@pytest.fixture
def demo_client(session: Session) -> Iterator[TestClient]:
    yield from api_client(session, "demo")


def test_api_needs_login(client: TestClient) -> None:
    client.cookies.clear()
    assert client.get("/digest").status_code == 401
    assert client.get("/items").status_code == 401


def test_api_digest_is_null_before_the_first_one(client: TestClient) -> None:
    resp = client.get("/digest")
    assert resp.status_code == 200
    assert resp.json() is None


def test_api_digest_has_evidence_and_gmail_link(client: TestClient, session: Session) -> None:
    seed_item(session, "Homework 3", MORNING, due_at=MORNING + timedelta(days=2))
    make_digest(session, MORNING, LA, 7)
    body = client.get("/digest").json()
    assert body["digest_date"] == "2026-11-11"
    assert body["timezone"] == "America/Los_Angeles"
    (soon,) = body["due_soon"]
    assert soon["title"] == "Homework 3"
    assert soon["evidence"] == "Homework 3 is due soon."
    assert soon["subject"] == "CS 000: Homework 3"
    msg_id = session.execute(sa.select(Message.provider_msg_id)).scalar_one()
    expected = f"https://mail.google.com/mail/?authuser={EMAIL}#all/{msg_id}"
    assert soon["gmail_url"] == expected


def test_api_demo_mode_has_no_gmail_link(demo_client: TestClient, session: Session) -> None:
    seed_item(session, "Homework 3", MORNING, due_at=MORNING + timedelta(days=2))
    make_digest(session, MORNING, LA, 7)
    (soon,) = demo_client.get("/digest").json()["due_soon"]
    assert soon["gmail_url"] is None


def test_api_items_filter_by_status(client: TestClient, session: Session) -> None:
    seed_item(session, "Open one", MORNING, due_at=MORNING + timedelta(days=1))
    seed_item(session, "Review one", MORNING, status="needs_review", review="no_due_raw")
    seed_item(session, "Done one", MORNING, status="done")
    default = [i["title"] for i in client.get("/items").json()]
    assert default == ["Open one", "Review one"]
    done = client.get("/items", params={"status": "done"}).json()
    assert [i["title"] for i in done] == ["Done one"]
    review = client.get("/items", params={"status": "needs_review"}).json()
    assert review[0]["review"] == "no_due_raw"
    assert client.get("/items", params={"status": "bogus"}).status_code == 422


# ---- Worker job ----


def test_demo_waits_for_items_before_its_digest(session: Session) -> None:
    settings = Settings(sinan_mode="demo")
    assert digest_today(lambda: session, settings, clock=lambda: EARLY) is False
    assert latest(session) is None
    seed_item(session, "Soon", EARLY, due_at=EARLY + timedelta(days=1))
    assert digest_today(lambda: session, settings, clock=lambda: EARLY) is True


def test_live_waits_for_the_hour_even_with_items(session: Session) -> None:
    seed_item(session, "Soon", EARLY, due_at=EARLY + timedelta(days=1))
    live = Settings(sinan_mode="demo").model_copy(update={"sinan_mode": "live"})
    assert digest_today(lambda: session, live, clock=lambda: EARLY) is False
    assert digest_today(lambda: session, live, clock=lambda: MORNING) is True
