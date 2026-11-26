"""Due times the owner sets, and Confirm (ADR 0022, decision 3).

Seeded synthetic data only. Every course, person and address is fictional.
"""

import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.actions.proposals import NotPending, approve, effective
from app.db.digest import list_items
from app.db.item_due import ACTIVE, Refused, confirm, due_text, local_due, set_due
from app.db.item_status import UnknownItem
from app.db.models import AuditEvent, Item, ItemVersion, Proposal
from tests.test_digest import LA, MORNING, seed_item, seed_proposal
from tests.test_digest import client as client  # noqa: F401  (fixture)
from tests.test_digest import session as session  # noqa: F401  (fixture)

db = pytest.mark.db

DEC_1 = date(2026, 12, 1)
FIVE_PM = time(17, 0)


# ---- Pure ----


def test_no_time_is_end_of_day() -> None:
    assert local_due(DEC_1, None, LA) == datetime(2026, 12, 2, 7, 59, tzinfo=UTC)


def test_time_is_local() -> None:
    assert local_due(DEC_1, FIVE_PM, LA) == datetime(2026, 12, 2, 1, 0, tzinfo=UTC)


def test_due_is_utc() -> None:
    assert local_due(DEC_1, FIVE_PM, LA).tzinfo == UTC


@pytest.mark.parametrize(
    ("day", "at", "code"),
    [
        # Spring forward: 2:30 does not exist on Mar 14, 2027
        (date(2027, 3, 14), time(2, 30), "nonexistent_time"),
        # Fall back: 1:30 happens twice on Nov 7, 2027
        (date(2027, 11, 7), time(1, 30), "ambiguous_time"),
    ],
)
def test_clock_change_times_are_refused(day: date, at: time, code: str) -> None:
    with pytest.raises(Refused) as caught:
        local_due(day, at, LA)
    assert caught.value.code == code


def test_due_text() -> None:
    assert due_text(DEC_1, None) == "2026-12-01"
    assert due_text(DEC_1, FIVE_PM) == "2026-12-01 17:00"


# ---- Domain ----


def versions(session: Session, item_id: uuid.UUID) -> list[ItemVersion]:
    query = sa.select(ItemVersion).where(ItemVersion.item_id == item_id)
    ordered = query.order_by(ItemVersion.version)
    return list(session.execute(ordered).scalars())


def item(session: Session, item_id: uuid.UUID) -> Item:
    session.expire_all()
    return session.execute(sa.select(Item).where(Item.id == item_id)).scalar_one()


def events(session: Session, kind: str) -> list[AuditEvent]:
    query = sa.select(AuditEvent).where(AuditEvent.event_type == kind)
    return list(session.execute(query).scalars())


@db
def test_add_due_to_a_todo(session: Session) -> None:
    item_id = seed_item(session, "Lab 14", MORNING)
    set_due(session, item_id, DEC_1, FIVE_PM, LA)
    session.commit()
    found = item(session, item_id)
    assert found.due_at == datetime(2026, 12, 2, 1, 0, tzinfo=UTC)
    assert found.due_raw == "2026-12-01 17:00"
    assert found.status == "open"
    last = versions(session, item_id)[-1]
    assert last.version == 2
    assert last.change_type == "update"
    assert last.actor == "user"
    assert last.message_id is None
    assert last.before == {"due_raw": None, "due_at": None, "status": "open"}
    assert last.after == {
        "due_raw": "2026-12-01 17:00",
        "due_at": "2026-12-02T01:00:00Z",
        "status": "open",
        "review": None,
        "zone_assumed": False,
        "time_assumed": False,
        "date_computed": False,
        "time_carried": False,
    }


@db
def test_no_time_is_flagged_as_assumed(session: Session) -> None:
    item_id = seed_item(session, "Lab 14", MORNING)
    set_due(session, item_id, DEC_1, None, LA)
    session.commit()
    assert item(session, item_id).due_raw == "2026-12-01"
    assert versions(session, item_id)[-1].after["time_assumed"] is True
    view = next(v for v in list_items(session, ACTIVE) if v.id == item_id)
    assert view.assumed.time
    assert view.due_at == datetime(2026, 12, 2, 7, 59, tzinfo=UTC)


@db
def test_change_clears_review(session: Session) -> None:
    due = MORNING + timedelta(days=3)
    item_id = seed_item(session, "Lab 14", MORNING, due, "needs_review", "numeric_date")
    set_due(session, item_id, DEC_1, None, LA)
    session.commit()
    assert item(session, item_id).status == "open"
    last = versions(session, item_id)[-1]
    assert last.before == {
        "due_raw": "Friday 11:59pm PT",
        "due_at": "2026-11-14T15:30:00Z",
        "status": "needs_review",
    }
    view = next(v for v in list_items(session, ACTIVE) if v.id == item_id)
    assert view.review is None


@db
def test_a_made_up_suggestion_id_is_refused(session: Session) -> None:
    """Only a real suggestion for this item can be recorded (ADR 0022);
    tests/test_suggest.py covers a real one."""
    item_id = seed_item(session, "Lab 14", MORNING)
    with pytest.raises(Refused) as caught:
        set_due(session, item_id, DEC_1, None, LA, uuid.uuid4())
    assert caught.value.code == "bad_suggestion"


@db
def test_due_is_audited_without_content(session: Session) -> None:
    item_id = seed_item(session, "Lab 14", MORNING)
    set_due(session, item_id, DEC_1, FIVE_PM, LA)
    session.commit()
    (event,) = events(session, "item.due")
    assert event.actor == "user"
    assert event.subject_id == item_id
    assert event.data == {"from": "open", "to": "open", "version": 2}


@db
@pytest.mark.parametrize("closed", ["done", "dismissed"])
def test_closed_item_is_refused(session: Session, closed: str) -> None:
    item_id = seed_item(session, "Lab 14", MORNING, status=closed)
    with pytest.raises(Refused) as caught:
        set_due(session, item_id, DEC_1, None, LA)
    assert caught.value.code == "closed"
    session.rollback()
    assert len(versions(session, item_id)) == 1


@db
def test_notice_is_refused(session: Session) -> None:
    item_id = seed_item(session, "Talk", MORNING)
    session.execute(sa.update(Item).where(Item.id == item_id).values(kind="notice"))
    session.commit()
    with pytest.raises(Refused) as caught:
        set_due(session, item_id, DEC_1, None, LA)
    assert caught.value.code == "notice"


@db
def test_clock_change_writes_nothing(session: Session) -> None:
    item_id = seed_item(session, "Lab 14", MORNING)
    with pytest.raises(Refused):
        set_due(session, item_id, date(2027, 3, 14), time(2, 30), LA)
    session.rollback()
    assert len(versions(session, item_id)) == 1
    assert events(session, "item.due") == []


@db
def test_unknown_item(session: Session) -> None:
    with pytest.raises(UnknownItem):
        set_due(session, uuid.uuid4(), DEC_1, None, LA)
    with pytest.raises(UnknownItem):
        confirm(session, uuid.uuid4())


@db
def test_owner_change_supersedes_a_pending_proposal(session: Session) -> None:
    due = MORNING + timedelta(days=3)
    item_id = seed_item(session, "Lab 14", MORNING, due)
    seed_proposal(session, item_id, MORNING + timedelta(days=5))
    proposal = session.execute(sa.select(Proposal)).scalar_one()
    assert effective(session, proposal, MORNING) == "pending"
    set_due(session, item_id, DEC_1, None, LA)
    session.commit()
    assert effective(session, proposal, MORNING) == "superseded"
    with pytest.raises(NotPending) as caught:
        approve(session, proposal.id, proposal.payload_hash, MORNING)
    assert caught.value.status == "superseded"


@db
def test_confirm_opens_a_review_item(session: Session) -> None:
    due = MORNING + timedelta(days=3)
    item_id = seed_item(session, "Lab 14", MORNING, due, "needs_review", "numeric_date")
    confirm(session, item_id)
    session.commit()
    assert item(session, item_id).status == "open"
    assert item(session, item_id).due_at == due
    last = versions(session, item_id)[-1]
    assert last.change_type == "status"
    assert last.actor == "user"
    assert last.before == {"status": "needs_review"}
    assert last.after == {"status": "open", "review": None}
    (event,) = events(session, "item.confirm")
    assert event.data == {"from": "needs_review", "to": "open", "version": 2}
    view = next(v for v in list_items(session, ACTIVE) if v.id == item_id)
    assert view.review is None


@db
def test_confirm_without_due_is_refused(session: Session) -> None:
    item_id = seed_item(session, "Lab 14", MORNING, status="needs_review", review="no_due_raw")
    with pytest.raises(Refused) as caught:
        confirm(session, item_id)
    assert caught.value.code == "no_due"


@db
def test_confirm_needs_a_review_item(session: Session) -> None:
    item_id = seed_item(session, "Lab 14", MORNING, MORNING + timedelta(days=3))
    with pytest.raises(Refused) as caught:
        confirm(session, item_id)
    assert caught.value.code == "not_review"


# ---- API ----


@db
def test_api_sets_due(client: TestClient, session: Session) -> None:
    item_id = seed_item(session, "Lab 14", MORNING)
    resp = client.post(f"/items/{item_id}/due", json={"date": "2026-12-01", "time": "17:00"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"due_at": "2026-12-02T01:00:00Z", "time_assumed": False, "version": 2}


@db
def test_api_without_time(client: TestClient, session: Session) -> None:
    item_id = seed_item(session, "Lab 14", MORNING)
    resp = client.post(f"/items/{item_id}/due", json={"date": "2026-12-01"})
    assert resp.status_code == 200
    assert resp.json()["time_assumed"] is True


@db
@pytest.mark.parametrize(
    "body",
    [
        {"date": "2026-02-30"},
        {"date": "2026-12-01", "time": "25:00"},
        {"date": "2026-12-01", "time": "17:00:30"},
        {"date": "2026-12-01", "time": "17:00+02:00"},
        {"date": "Dec 1"},
        {},
    ],
)
def test_api_refuses_bad_input(client: TestClient, session: Session, body: dict[str, str]) -> None:
    item_id = seed_item(session, "Lab 14", MORNING)
    resp = client.post(f"/items/{item_id}/due", json=body)
    assert resp.status_code == 422
    assert len(versions(session, item_id)) == 1


@db
def test_api_refuses_clock_change_time(client: TestClient, session: Session) -> None:
    item_id = seed_item(session, "Lab 14", MORNING)
    resp = client.post(f"/items/{item_id}/due", json={"date": "2027-03-14", "time": "02:30"})
    assert resp.status_code == 422
    assert "clock change" in resp.json()["detail"]


@db
def test_api_refuses_closed_item(client: TestClient, session: Session) -> None:
    item_id = seed_item(session, "Lab 14", MORNING, status="done")
    resp = client.post(f"/items/{item_id}/due", json={"date": "2026-12-01"})
    assert resp.status_code == 409


@db
def test_api_confirms(client: TestClient, session: Session) -> None:
    due = MORNING + timedelta(days=3)
    item_id = seed_item(session, "Lab 14", MORNING, due, "needs_review", "numeric_date")
    resp = client.post(f"/items/{item_id}/confirm")
    assert resp.status_code == 200
    assert resp.json() == {"status": "open", "version": 2}


@db
def test_api_refuses_confirm_without_due(client: TestClient, session: Session) -> None:
    item_id = seed_item(session, "Lab 14", MORNING, status="needs_review", review="no_due_raw")
    resp = client.post(f"/items/{item_id}/confirm")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "The item has no due time; set one instead"


@db
def test_api_unknown_item(client: TestClient) -> None:
    missing = uuid.uuid4()
    assert client.post(f"/items/{missing}/due", json={"date": "2026-12-01"}).status_code == 404
    assert client.post(f"/items/{missing}/confirm").status_code == 404


def test_api_needs_login() -> None:
    from apps.api.main import app

    with TestClient(app) as anonymous:
        missing = uuid.uuid4()
        resp = anonymous.post(f"/items/{missing}/due", json={"date": "2026-12-01"})
        assert resp.status_code == 401
        assert anonymous.post(f"/items/{missing}/confirm").status_code == 401
