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
from app.db.digest import latest, make_digest, past_items, read_digest
from app.db.models import (
    Account,
    AuditEvent,
    Digest,
    DigestItem,
    ExtractionRun,
    Item,
    ItemVersion,
    Message,
    Proposal,
)
from app.extraction.prompt import PROMPT_HASH, PROMPT_VERSION
from app.proposals import payload_hash
from apps.api.auth import COOKIE, get_auth
from apps.api.clock import get_clock
from apps.api.main import app
from apps.api.oauth import get_session
from apps.worker.sweep_job import sweep_proposals

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
    view = read_digest(session, found, MORNING)
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
    # Every active item counts as new in the first digest, except overdue "Past"
    assert sections(session) == {
        "new": ["Soon", "Later", "Check"],
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
    expected = {"date": "2026-11-11", "new": 1, "due_soon": 1, "needs_review": 0}
    assert event.data == {**expected, "notices": 0, "expired_proposals": 0}


def test_digest_shows_current_item_state(session: Session) -> None:
    item_id = seed_item(session, "Soon", MORNING, due_at=MORNING + timedelta(days=1))
    make_digest(session, MORNING, LA, 7)
    session.execute(sa.update(Item).where(Item.id == item_id).values(title="Renamed"))
    session.commit()
    assert sections(session)["due_soon"] == ["Renamed"]


# ---- Time on read (ADR 0015) ----

DAY = timedelta(days=1)


def seed_proposal(session: Session, item_id: uuid.UUID, expires_at: datetime) -> None:
    """A pending proposal on the item's current version, from its source email."""
    first = session.execute(sa.select(ItemVersion).where(ItemVersion.item_id == item_id))
    version = first.scalar_one()
    payload = {"due_raw": "Monday 11:59pm PT", "due_at": (MORNING + DAY).isoformat()}
    proposal = Proposal(
        kind="item_update",
        item_id=item_id,
        base_version=version.version,
        payload=payload,
        payload_hash=payload_hash(payload),
        message_id=version.message_id,
        extraction_run_id=version.extraction_run_id,
        expires_at=expires_at,
    )
    session.add(proposal)
    session.commit()


def read_at(session: Session, now: datetime) -> dict[str, list[str]]:
    found = latest(session)
    assert found is not None
    view = read_digest(session, found, now)
    names = {name: [i.title for i in items] for name, items in view.sections.items()}
    names["overdue"] = [i.title for i in view.overdue]
    past = past_items(session, now)
    names["past"] = [i.title for i in past.past]
    names["stale"] = [i.title for i in past.stale]
    return names


def stored(session: Session) -> dict[str, int]:
    query = sa.select(DigestItem.section, sa.func.count()).group_by(DigestItem.section)
    return {section: count for section, count in session.execute(query).all()}


def test_past_and_stale_leave_the_digest(session: Session) -> None:
    seed_item(session, "Due tomorrow", MORNING - DAY, due_at=MORNING + DAY)
    seed_item(session, "Due yesterday", MORNING - 5 * DAY, due_at=MORNING - DAY)
    seed_item(session, "Due 3 days ago", MORNING - 5 * DAY, due_at=MORNING - 3 * DAY)
    seed_item(session, "Old review", MORNING - 15 * DAY, status="needs_review")
    seed_item(session, "Recent review", MORNING - 13 * DAY, status="needs_review")
    make_digest(session, MORNING, LA, 7)
    assert read_at(session, MORNING) == {
        "new": ["Due tomorrow", "Recent review"],
        "due_soon": ["Due tomorrow"],
        "needs_review": ["Recent review"],
        "overdue": ["Due yesterday"],
        "past": ["Due 3 days ago"],
        "stale": ["Old review"],
    }


def test_nothing_is_restatused(session: Session) -> None:
    seed_item(session, "Due 3 days ago", MORNING - 5 * DAY, due_at=MORNING - 3 * DAY)
    seed_item(session, "Old review", MORNING - 15 * DAY, status="needs_review")
    make_digest(session, MORNING, LA, 7)
    read_at(session, MORNING)
    statuses = session.execute(sa.select(Item.title, Item.status)).all()
    assert sorted(statuses) == [("Due 3 days ago", "open"), ("Old review", "needs_review")]


def test_item_already_past_when_extracted_is_not_new(session: Session) -> None:
    make_digest(session, MORNING - DAY, LA, 7)
    # Found in old mail an hour ago, for an exam a month back
    found = MORNING - timedelta(hours=1)
    seed_item(session, "Old midterm", found, due_at=MORNING - 30 * DAY)
    seed_item(session, "Fresh quiz", found, due_at=MORNING + 3 * DAY)
    make_digest(session, MORNING, LA, 7)
    names = read_at(session, MORNING)
    assert names["new"] == ["Fresh quiz"]
    assert names["past"] == ["Old midterm"]
    assert names["overdue"] == []


def test_pending_proposal_keeps_the_item_shown(session: Session) -> None:
    moved = seed_item(session, "Moved exam", MORNING - 5 * DAY, due_at=MORNING - 3 * DAY)
    review = seed_item(session, "Moved review", MORNING - 15 * DAY, status="needs_review")
    seed_proposal(session, moved, MORNING + DAY)
    seed_proposal(session, review, MORNING + DAY)
    make_digest(session, MORNING, LA, 7)
    names = read_at(session, MORNING)
    assert names["overdue"] == ["Moved exam"]
    assert names["needs_review"] == ["Moved review"]
    assert names["past"] == names["stale"] == []
    # Once the proposal expires, both leave the digest
    later = read_at(session, MORNING + 2 * DAY)
    assert later["past"] == ["Moved exam"]
    assert later["stale"] == ["Moved review"]
    assert later["overdue"] == later["needs_review"] == []


def test_stored_digest_holds_only_current_items(session: Session) -> None:
    seed_item(session, "Due tomorrow", MORNING - DAY, due_at=MORNING + DAY)
    seed_item(session, "Due yesterday", MORNING - 5 * DAY, due_at=MORNING - DAY)
    seed_item(session, "Old review", MORNING - 15 * DAY, status="needs_review")
    make_digest(session, MORNING, LA, 7)
    assert stored(session) == {"new": 1, "due_soon": 1}
    event = session.execute(sa.select(AuditEvent)).scalar_one()
    # The audited counts match the stored sections
    assert event.data["new"] == 1
    assert event.data["due_soon"] == 1
    assert event.data["needs_review"] == 0


def test_reading_later_moves_items_without_changing_the_digest(session: Session) -> None:
    seed_item(session, "Due tonight", MORNING - DAY, due_at=MORNING + timedelta(hours=8))
    make_digest(session, MORNING, LA, 7)
    before = stored(session)
    tomorrow = read_at(session, MORNING + DAY)
    assert tomorrow["due_soon"] == tomorrow["new"] == []
    assert tomorrow["overdue"] == ["Due tonight"]
    in_three_days = read_at(session, MORNING + 3 * DAY)
    assert in_three_days["overdue"] == []
    assert in_three_days["past"] == ["Due tonight"]
    assert stored(session) == before


def test_api_past_view(client: TestClient, session: Session) -> None:
    seed_item(session, "Due 3 days ago", MORNING - 5 * DAY, due_at=MORNING - 3 * DAY)
    seed_item(session, "Due 9 days ago", MORNING - 10 * DAY, due_at=MORNING - 9 * DAY)
    seed_item(session, "Old review", MORNING - 15 * DAY, status="needs_review")
    seed_item(session, "Due yesterday", MORNING - 5 * DAY, due_at=MORNING - DAY)
    body = client.get("/items/past").json()
    # Most recently due first
    assert [i["title"] for i in body["past"]] == ["Due 3 days ago", "Due 9 days ago"]
    assert [i["title"] for i in body["stale"]] == ["Old review"]
    assert body["stale"][0]["status"] == "needs_review"


def test_api_past_view_needs_login(client: TestClient) -> None:
    client.cookies.clear()
    assert client.get("/items/past").status_code == 401


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
    app.dependency_overrides[get_clock] = lambda: lambda: MORNING
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
    assert client.get("/home").status_code == 401
    assert client.get("/timezone").status_code == 401
    assert client.get("/items").status_code == 401


def test_the_digest_route_is_gone(client: TestClient) -> None:
    """The Dashboard reads /home now (ADR 0022)."""
    assert client.get("/digest").status_code == 404


def test_api_timezone(client: TestClient) -> None:
    assert client.get("/timezone").json() == {"timezone": "America/Los_Angeles"}


def test_api_home_has_evidence_and_gmail_link(client: TestClient, session: Session) -> None:
    seed_item(session, "Homework 3", MORNING, due_at=MORNING + timedelta(days=2))
    body = client.get("/home").json()
    assert body["timezone"] == "America/Los_Angeles"
    (soon,) = body["due"]
    assert soon["title"] == "Homework 3"
    assert soon["evidence"] == "Homework 3 is due soon."
    assert soon["subject"] == "CS 000: Homework 3"
    msg_id = session.execute(sa.select(Message.provider_msg_id)).scalar_one()
    expected = f"https://mail.google.com/mail/?authuser={EMAIL}#all/{msg_id}"
    assert soon["gmail_url"] == expected


def test_api_demo_mode_has_no_gmail_link(demo_client: TestClient, session: Session) -> None:
    seed_item(session, "Homework 3", MORNING, due_at=MORNING + timedelta(days=2))
    (soon,) = demo_client.get("/home").json()["due"]
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


# ---- Worker ----


def test_worker_sweeps_and_makes_no_digest(session: Session) -> None:
    """The cycle's last step records expired proposals; no digest is made (ADR 0022)."""
    item_id = seed_item(session, "Soon", MORNING - DAY, due_at=MORNING + 3 * DAY)
    seed_proposal(session, item_id, MORNING - timedelta(hours=1))
    counts = sweep_proposals(lambda: session, clock=lambda: MORNING)
    assert counts["expired"] == 1
    assert latest(session) is None
    status = session.execute(sa.select(Proposal.status)).scalar_one()
    assert status == "expired"


def test_sweep_failure_is_logged_not_raised(session: Session) -> None:
    def broken() -> Session:
        raise RuntimeError("no database")

    assert sweep_proposals(broken, clock=lambda: MORNING) == {}
