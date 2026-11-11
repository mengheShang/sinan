"""Owner status changes on items, on seeded synthetic data (Issue #29)."""

import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.digest import latest, make_digest, read_digest
from app.db.item_status import BadMove, UnknownItem, allowed, set_status
from app.db.models import AuditEvent, Item, ItemVersion
from tests.test_digest import LA, MORNING, api_client, seed_item
from tests.test_digest import session as session  # noqa: F401  (fixture)

db = pytest.mark.db

MOVES = [
    ("open", "done"),
    ("open", "dismissed"),
    ("needs_review", "done"),
    ("needs_review", "dismissed"),
    ("done", "open"),
    ("dismissed", "open"),
]

REFUSED = [
    ("open", "open"),
    ("open", "needs_review"),
    ("needs_review", "open"),
    ("done", "dismissed"),
    ("dismissed", "done"),
    ("done", "done"),
]


@pytest.mark.parametrize(("old", "new"), MOVES)
def test_allowed_moves(old: str, new: str) -> None:
    assert allowed(old, new)


@pytest.mark.parametrize(("old", "new"), REFUSED)
def test_refused_moves(old: str, new: str) -> None:
    assert not allowed(old, new)


def versions(session: Session, item_id: uuid.UUID) -> list[ItemVersion]:
    query = sa.select(ItemVersion).where(ItemVersion.item_id == item_id)
    ordered = query.order_by(ItemVersion.version)
    return list(session.execute(ordered).scalars())


def status_of(session: Session, item_id: uuid.UUID) -> str:
    query = sa.select(Item.status).where(Item.id == item_id)
    return session.execute(query).scalar_one()


# ---- Domain ----


@db
@pytest.mark.parametrize(("old", "new"), MOVES)
def test_each_move_updates_item_and_appends_version(session: Session, old: str, new: str) -> None:
    item_id = seed_item(session, "Homework 3", MORNING, status=old)
    set_status(session, item_id, new)
    session.commit()
    assert status_of(session, item_id) == new
    first, second = versions(session, item_id)
    assert first.version == 1
    assert second.version == 2
    assert second.change_type == "status"
    assert second.actor == "user"
    assert second.before == {"status": old}
    assert second.after == {"status": new}
    assert second.message_id is None
    assert second.evidence is None


@db
def test_refused_move_changes_nothing(session: Session) -> None:
    item_id = seed_item(session, "Homework 3", MORNING, status="done")
    with pytest.raises(BadMove):
        set_status(session, item_id, "dismissed")
    session.rollback()
    assert status_of(session, item_id) == "done"
    assert len(versions(session, item_id)) == 1


@db
def test_unknown_item(session: Session) -> None:
    with pytest.raises(UnknownItem):
        set_status(session, uuid.uuid4(), "done")


@db
def test_versions_keep_increasing(session: Session) -> None:
    item_id = seed_item(session, "Homework 3", MORNING)
    for new in ("done", "open", "dismissed", "open"):
        set_status(session, item_id, new)
        session.commit()
    numbers = [v.version for v in versions(session, item_id)]
    assert numbers == [1, 2, 3, 4, 5]


@db
def test_change_is_audited_without_content(session: Session) -> None:
    item_id = seed_item(session, "Homework 3", MORNING)
    set_status(session, item_id, "done")
    session.commit()
    event = session.execute(sa.select(AuditEvent)).scalar_one()
    assert event.event_type == "item.status"
    assert event.actor == "user"
    assert event.subject_type == "item"
    assert event.subject_id == item_id
    assert event.data == {"from": "open", "to": "done", "version": 2}


@db
def test_status_version_stays_append_only(session: Session) -> None:
    item_id = seed_item(session, "Homework 3", MORNING)
    set_status(session, item_id, "done")
    session.commit()
    where = (ItemVersion.item_id == item_id) & (ItemVersion.version == 2)
    with pytest.raises(sa.exc.DBAPIError):
        session.execute(sa.update(ItemVersion).where(where).values(after={"status": "open"}))
    session.rollback()


@db
def test_later_digest_leaves_out_closed_item(session: Session) -> None:
    due = MORNING + timedelta(days=2)
    item_id = seed_item(session, "Homework 3", MORNING - timedelta(days=1), due_at=due)
    first = make_digest(session, MORNING - timedelta(days=1), LA, 7)
    assert first is not None
    set_status(session, item_id, "done")
    session.commit()
    second = make_digest(session, MORNING, LA, 7)
    assert second is not None
    assert latest(session) == second
    later = read_digest(session, second)
    assert later.sections["due_soon"] == []
    earlier = read_digest(session, first)
    titles = [v.title for v in earlier.sections["due_soon"]]
    assert titles == ["Homework 3"]
    assert earlier.sections["due_soon"][0].status == "done"


# ---- API ----


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    yield from api_client(session, "live")


def post(client: TestClient, item_id: uuid.UUID | str, new: str) -> tuple[int, dict[str, str]]:
    resp = client.post(f"/items/{item_id}/status", json={"status": new})
    return resp.status_code, resp.json()


@db
def test_api_marks_done_and_reopens(client: TestClient, session: Session) -> None:
    item_id = seed_item(session, "Homework 3", MORNING)
    assert post(client, item_id, "done") == (200, {"status": "done", "version": 2})
    assert status_of(session, item_id) == "done"
    assert post(client, item_id, "open") == (200, {"status": "open", "version": 3})
    assert status_of(session, item_id) == "open"


@db
def test_api_dismisses_review_item(client: TestClient, session: Session) -> None:
    item_id = seed_item(session, "Check", MORNING, status="needs_review", review="numeric_date")
    code, _ = post(client, item_id, "dismissed")
    assert code == 200
    assert status_of(session, item_id) == "dismissed"


@db
def test_api_refuses_move(client: TestClient, session: Session) -> None:
    item_id = seed_item(session, "Homework 3", MORNING, status="done")
    code, body = post(client, item_id, "dismissed")
    assert code == 409
    assert body == {"detail": "Cannot move from done to dismissed"}
    assert status_of(session, item_id) == "done"
    assert len(versions(session, item_id)) == 1


@db
def test_api_refuses_needs_review_as_target(client: TestClient, session: Session) -> None:
    item_id = seed_item(session, "Homework 3", MORNING)
    code, _ = post(client, item_id, "needs_review")
    assert code == 422


@db
def test_api_unknown_item(client: TestClient) -> None:
    code, _ = post(client, uuid.uuid4(), "done")
    assert code == 404
    code, _ = post(client, "not-a-uuid", "done")
    assert code == 422


@db
def test_api_needs_login(client: TestClient, session: Session) -> None:
    item_id = seed_item(session, "Homework 3", MORNING)
    client.cookies.clear()
    code, _ = post(client, item_id, "done")
    assert code == 401
    assert status_of(session, item_id) == "open"
