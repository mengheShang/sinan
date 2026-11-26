"""The live home page (ADR 0022, decisions 1 and 2).

Seeded synthetic data only. Every course, person and address is fictional.
"""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.digest import latest
from app.db.home import HomeView, is_added_today, read_home
from app.db.models import AuditEvent, Item, ItemVersion
from tests.test_digest import LA, MORNING, seed_item, seed_proposal
from tests.test_digest import client as client  # noqa: F401  (fixture)
from tests.test_digest import session as session  # noqa: F401  (fixture)

db = pytest.mark.db

DAY = timedelta(days=1)
HOUR = timedelta(hours=1)


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ---- Added today (pure) ----

# MORNING is Wed Nov 11 2026, 07:30 in Los Angeles (PST, UTC-8)
ADDED_TODAY = [
    # Local midnight is 08:00 UTC
    (utc(2026, 11, 11, 7, 59), MORNING, False),
    (utc(2026, 11, 11, 8, 0), MORNING, True),
    (utc(2026, 11, 11, 15, 0), MORNING, True),
    # Late on Nov 11 in Los Angeles is already Nov 12 in UTC
    (utc(2026, 11, 12, 7, 59), utc(2026, 11, 12, 7, 59), True),
    (utc(2026, 11, 12, 7, 59), utc(2026, 11, 12, 8, 0), False),
    # The fall-back day: Nov 1 2026 starts at 07:00 UTC (PDT) and ends at
    # 08:00 UTC Nov 2 (PST), so it is 25 hours long
    (utc(2026, 11, 1, 6, 59), utc(2026, 11, 1, 12, 0), False),
    (utc(2026, 11, 1, 7, 0), utc(2026, 11, 2, 7, 59), True),
    (utc(2026, 11, 2, 8, 0), utc(2026, 11, 2, 7, 59), False),
    # The spring-forward day: Mar 14 2027 starts at 08:00 UTC (PST) and ends
    # at 07:00 UTC Mar 15 (PDT), so it is 23 hours long
    (utc(2027, 3, 14, 8, 0), utc(2027, 3, 15, 6, 59), True),
    (utc(2027, 3, 14, 7, 59), utc(2027, 3, 15, 6, 59), False),
    (utc(2027, 3, 14, 8, 0), utc(2027, 3, 15, 7, 0), False),
]


@pytest.mark.parametrize(("created", "now", "expected"), ADDED_TODAY)
def test_added_today(created: datetime, now: datetime, expected: bool) -> None:
    assert is_added_today(created, now, LA) is expected


# ---- Lists ----


def titles(view: HomeView) -> dict[str, list[str]]:
    return {
        "due": [e.view.title for e in view.due],
        "todo": [e.view.title for e in view.todo],
        "notices": [e.view.title for e in view.notices],
    }


def notice(session: Session, title: str, created: datetime) -> None:
    item_id = seed_item(session, title, created)
    session.execute(sa.update(Item).where(Item.id == item_id).values(kind="notice"))
    session.commit()


@db
def test_each_active_item_is_in_one_list(session: Session) -> None:
    seed_item(session, "Soon", MORNING - DAY, MORNING + DAY)
    seed_item(session, "Later", MORNING - DAY, MORNING + 20 * DAY)
    seed_item(session, "Yesterday", MORNING - 5 * DAY, MORNING - DAY)
    seed_item(
        session, "Check due", MORNING - DAY, MORNING + 2 * DAY, "needs_review", "numeric_date"
    )
    seed_item(session, "Check sender", MORNING - DAY, None, "needs_review", "untrusted_sender")
    seed_item(session, "Read chapter", MORNING - DAY)
    seed_item(session, "Done one", MORNING - DAY, MORNING + DAY, "done")
    seed_item(session, "Long past", MORNING - 10 * DAY, MORNING - 5 * DAY)
    notice(session, "Flu shots", MORNING - DAY)
    found = titles(read_home(session, MORNING, LA))
    assert found == {
        "due": ["Yesterday", "Soon", "Later", "Check due", "Check sender"],
        "todo": ["Read chapter"],
        "notices": ["Flu shots"],
    }
    listed = [t for names in found.values() for t in names]
    assert len(listed) == len(set(listed))


@db
def test_overdue_is_flagged_and_on_top(session: Session) -> None:
    seed_item(session, "Soon", MORNING - DAY, MORNING + HOUR)
    seed_item(session, "An hour ago", MORNING - 5 * DAY, MORNING - HOUR)
    seed_item(session, "Yesterday", MORNING - 5 * DAY, MORNING - DAY)
    due = read_home(session, MORNING, LA).due
    assert [e.view.title for e in due] == ["Yesterday", "An hour ago", "Soon"]
    assert [e.overdue for e in due] == [True, True, False]


@db
def test_past_and_stale_items_stay_in_the_past_view(session: Session) -> None:
    """ADR 0015: two days past due, or stale review, leaves the home page."""
    seed_item(session, "Three days ago", MORNING - 5 * DAY, MORNING - 3 * DAY)
    seed_item(session, "Old review", MORNING - 15 * DAY, None, "needs_review", "no_due_raw")
    found = titles(read_home(session, MORNING, LA))
    assert found == {"due": [], "todo": [], "notices": []}


@db
def test_an_overdue_review_item_stays_with_overdue(session: Session) -> None:
    seed_item(
        session, "Late check", MORNING - 5 * DAY, MORNING - DAY, "needs_review", "numeric_date"
    )
    seed_item(session, "Soon", MORNING - DAY, MORNING + DAY)
    due = read_home(session, MORNING, LA).due
    assert [e.view.title for e in due] == ["Late check", "Soon"]
    assert due[0].overdue


@db
def test_review_items_come_last_soonest_first(session: Session) -> None:
    seed_item(session, "No due", MORNING - DAY, None, "needs_review", "untrusted_sender")
    seed_item(session, "Check later", MORNING - DAY, MORNING + 5 * DAY, "needs_review", "x")
    seed_item(session, "Check sooner", MORNING - DAY, MORNING + 2 * DAY, "needs_review", "x")
    seed_item(session, "Far away", MORNING - DAY, MORNING + 30 * DAY)
    due = [e.view.title for e in read_home(session, MORNING, LA).due]
    assert due == ["Far away", "Check sooner", "Check later", "No due"]


@db
def test_todo_is_newest_first(session: Session) -> None:
    seed_item(session, "Older", MORNING - 3 * DAY)
    seed_item(session, "Newer", MORNING - DAY)
    todo = [e.view.title for e in read_home(session, MORNING, LA).todo]
    assert todo == ["Newer", "Older"]


@db
def test_a_due_time_moves_a_todo_to_due(session: Session) -> None:
    from datetime import date

    from app.db.item_due import set_due

    item_id = seed_item(session, "Lab 14", MORNING - DAY)
    assert titles(read_home(session, MORNING, LA))["todo"] == ["Lab 14"]
    set_due(session, item_id, date(2026, 12, 3), None, LA)
    session.commit()
    found = titles(read_home(session, MORNING, LA))
    assert found["todo"] == []
    assert found["due"] == ["Lab 14"]


@db
def test_added_today_in_every_list(session: Session) -> None:
    seed_item(session, "New due", MORNING - HOUR, MORNING + DAY)
    seed_item(session, "Old due", MORNING - DAY, MORNING + 2 * DAY)
    seed_item(session, "New todo", MORNING - HOUR)
    notice(session, "New notice", MORNING - HOUR)
    view = read_home(session, MORNING, LA)
    assert [(e.view.title, e.added_today) for e in view.due] == [
        ("New due", True),
        ("Old due", False),
    ]
    assert view.todo[0].added_today
    assert view.notices[0].added_today


@db
def test_a_held_item_stays_overdue(session: Session) -> None:
    """ADR 0015: a pending proposal keeps an item on the page after two days."""
    item_id = seed_item(session, "Held", MORNING - 10 * DAY, MORNING - 5 * DAY)
    seed_proposal(session, item_id, MORNING + DAY)
    (entry,) = read_home(session, MORNING, LA).due
    assert entry.view.title == "Held"
    assert entry.overdue


@db
def test_expired_proposals_in_the_last_week(session: Session) -> None:
    first = seed_item(session, "One", MORNING - 20 * DAY, MORNING + DAY)
    second = seed_item(session, "Two", MORNING - 20 * DAY, MORNING + DAY)
    seed_proposal(session, first, MORNING - 2 * DAY)
    seed_proposal(session, second, MORNING - 10 * DAY)
    session.execute(sa.text("UPDATE proposals SET status = 'expired'"))
    session.commit()
    assert read_home(session, MORNING, LA).expired_proposals == 1


@db
def test_reading_writes_nothing(session: Session) -> None:
    seed_item(session, "Soon", MORNING - DAY, MORNING + DAY)
    count = sa.select(sa.func.count())
    before = (
        session.execute(count.select_from(ItemVersion)).scalar_one(),
        session.execute(count.select_from(AuditEvent)).scalar_one(),
    )
    read_home(session, MORNING, LA)
    after = (
        session.execute(count.select_from(ItemVersion)).scalar_one(),
        session.execute(count.select_from(AuditEvent)).scalar_one(),
    )
    assert after == before
    assert latest(session) is None


# ---- API ----


@db
def test_api_home(client: TestClient, session: Session) -> None:
    seed_item(session, "Yesterday", MORNING - 5 * DAY, MORNING - DAY)
    seed_item(session, "Read chapter", MORNING - HOUR)
    notice(session, "Flu shots", MORNING - DAY)
    body = client.get("/home").json()
    assert body["timezone"] == "America/Los_Angeles"
    (late,) = body["due"]
    assert late["title"] == "Yesterday"
    assert late["overdue"] is True
    assert late["added_today"] is False
    (todo,) = body["todo"]
    assert todo["added_today"] is True
    (group,) = body["notice_groups"]
    assert [n["title"] for n in group["notices"]] == ["Flu shots"]
    assert body["expired_proposals"] == 0


@db
def test_other_lists_have_no_home_tags(client: TestClient, session: Session) -> None:
    seed_item(session, "Yesterday", MORNING - 5 * DAY, MORNING - DAY)
    (item,) = client.get("/items").json()
    assert item["overdue"] is False
    assert item["added_today"] is False
