"""The labeling API (Issues #40 and #41) on fictional synthetic mail."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.auth import Auth, LoginLimiter, SessionSigner, hash_password
from app.config import Settings, get_settings
from app.db import labels as store
from app.db import sampling
from app.db.models import Account, AuditEvent, EvalLabel, Message
from apps.api.auth import COOKIE, get_auth
from apps.api.main import app
from apps.api.oauth import get_session

pytestmark = pytest.mark.db

SECRET = "fake-session-secret-for-tests-only-0123456789"
# A marker that must never show up in an audit event or an error
MARKER = "Zebra-marker-7341"
HW_BODY = f"Homework 4 is due Friday Nov 20 at 11:59pm PT. {MARKER}"
EXT_BODY = f"Homework 4 is now due Monday Nov 23 at 11:59pm PT. {MARKER}"
NEWS_BODY = f"This week on campus: a book fair. {MARKER}"


@pytest.fixture
def session(conn: Connection) -> Iterator[Session]:
    with Session(bind=conn, join_transaction_mode="create_savepoint") as s:
        yield s


def at(day: int) -> datetime:
    return datetime(2026, 11, day, 17, 0, tzinfo=UTC)


def add_message(session: Session, acct: Account, day: int, subject: str, body: str) -> uuid.UUID:
    msg = Message(
        account_id=acct.id,
        provider_msg_id=f"syn-{uuid.uuid4().hex[:8]}",
        received_at=at(day),
        sent_at=at(day),
        from_addr="rivera@example.edu",
        subject=subject,
        body_text=body,
        headers={"Precedence": "bulk", "To": "class@example.edu"},
    )
    session.add(msg)
    session.flush()
    return msg.id


def pick(session: Session, message_id: uuid.UUID, eval_set: str, version: int = 1) -> None:
    """Record a draw of one email, as apps/worker/sample.py does."""
    data = {"eval_set": eval_set, "set_version": version, "role": "school", "stratum": "passed"}
    event = AuditEvent(
        event_type=sampling.PICK_EVENT,
        actor="user",
        subject_type="message",
        subject_id=message_id,
        data=data,
    )
    session.add(event)
    session.flush()


@pytest.fixture
def mail(session: Session) -> dict[str, uuid.UUID]:
    acct = Account(provider="gmail", email="student@example.edu", role="school")
    session.add(acct)
    session.flush()
    found = {
        "hw": add_message(session, acct, 2, "CS 000: Homework 4", HW_BODY),
        "news": add_message(session, acct, 3, "Campus weekly", NEWS_BODY),
        "ext": add_message(session, acct, 4, "CS 000: Homework 4 extended", EXT_BODY),
        # Synced but not sampled: never in the queue
        "other": add_message(session, acct, 1, "Club meeting", NEWS_BODY),
    }
    for name in ("hw", "news", "ext"):
        pick(session, found[name], "dev")
    # Releases the savepoint, so an API rollback keeps the mail; the test's
    # outer transaction still rolls everything back
    session.commit()
    return found


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    password_hash = hash_password("unused", n=2**4)
    signer = SessionSigner(SECRET, "owner", password_hash)
    auth = Auth("owner", password_hash, signer, LoginLimiter())
    settings = Settings(sinan_mode="demo", sinan_timezone="America/Los_Angeles")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_auth] = lambda: auth
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        c.cookies.set(COOKIE, signer.issue(auth.clock()))
        yield c
    app.dependency_overrides.clear()


def hw_item(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "ref": "cs000-hw4",
        "kind": "deadline",
        "course": "CS 000",
        "title": "Homework 4",
        "due_raw": "Friday Nov 20 at 11:59pm PT",
        "due_at": "2026-11-21T07:59:00Z",
        "due_ambiguous": False,
        "changes": None,
    }
    data.update(over)
    return data


def body(*items: dict[str, Any], **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "labels": {"items": list(items), "safe_to_auto_read": False},
        "tags": ["course"],
    }
    data.update(over)
    return data


def counts(**over: int) -> dict[str, int]:
    data = {"dev": 0, "test": 0, "dev_total": 3, "test_total": 0, "skipped": 0, "remaining": 0}
    data.update(over)
    return data


def audits(session: Session) -> list[AuditEvent]:
    query = sa.select(AuditEvent).where(AuditEvent.event_type.like("eval_label.%"))
    return list(session.execute(query).scalars())


def no_content(events: list[AuditEvent]) -> None:
    for event in events:
        text = repr(event.data)
        assert MARKER not in text
        assert "Homework" not in text
        assert "example.edu" not in text


# ---- Login ----


def test_needs_login(client: TestClient, mail: dict[str, uuid.UUID]) -> None:
    client.cookies.clear()
    hw = mail["hw"]
    assert client.get("/labeling").status_code == 401
    assert client.get(f"/labeling/emails/{hw}").status_code == 401
    assert client.post(f"/labeling/emails/{hw}/label", json=body()).status_code == 401
    assert client.post(f"/labeling/emails/{hw}/skip").status_code == 401
    fake = uuid.uuid4()
    assert client.post(f"/labeling/labels/{fake}/correct", json=body()).status_code == 401


# ---- Reading ----


def test_queue_starts_with_the_oldest(client: TestClient, mail: dict[str, uuid.UUID]) -> None:
    data = client.get("/labeling").json()
    assert data["counts"] == counts(remaining=3)
    assert data["next_open_id"] == str(mail["hw"])
    assert data["first_id"] == str(mail["hw"])


def test_email_shows_content_and_neighbours(client: TestClient, mail: dict[str, uuid.UUID]) -> None:
    data = client.get(f"/labeling/emails/{mail['news']}").json()
    assert data["from_addr"] == "rivera@example.edu"
    assert data["subject"] == "Campus weekly"
    assert data["body_text"] == NEWS_BODY
    assert data["role"] == "school"
    assert data["sent_at"].startswith("2026-11-03T17:00:00")
    assert data["frozen"] is False
    assert data["label_id"] is None
    assert data["prev_id"] == str(mail["hw"])
    assert data["next_id"] == str(mail["ext"])
    assert data["problems"] == []


def test_unknown_email(client: TestClient, mail: dict[str, uuid.UUID]) -> None:
    fake = uuid.uuid4()
    assert client.get(f"/labeling/emails/{fake}").status_code == 404
    assert client.post(f"/labeling/emails/{fake}/label", json=body(hw_item())).status_code == 404
    assert client.post(f"/labeling/emails/{fake}/skip").status_code == 404
    fixed = {"labels": body(hw_item())["labels"]}
    resp = client.post(f"/labeling/labels/{fake}/correct", json=fixed)
    assert resp.status_code == 404


# ---- Save ----


def test_save_freezes_and_stores(
    client: TestClient, session: Session, mail: dict[str, uuid.UUID]
) -> None:
    resp = client.post(f"/labeling/emails/{mail['hw']}/label", json=body(hw_item()))
    assert resp.status_code == 200
    data = resp.json()
    assert data["counts"] == counts(dev=1, remaining=2)
    assert data["next_open_id"] == str(mail["news"])
    row = session.get(EvalLabel, uuid.UUID(data["label_id"]))
    assert row is not None
    assert row.eval_set == "dev"
    assert row.set_version == 1
    assert row.source_message_id == mail["hw"]
    # Only the prefilter's headers are frozen
    assert row.snapshot["headers"] == {"Precedence": "bulk"}
    assert row.snapshot["body_text"] == HW_BODY
    assert row.labels["items"][0]["ref"] == "cs000-hw4"
    assert row.tags == ["course"]
    (event,) = audits(session)
    assert event.event_type == "eval_label.add"
    assert event.subject_id == row.id
    assert event.data == {"message_id": str(mail["hw"]), "eval_set": "dev", "set_version": 1}
    no_content([event])


def test_saved_email_shows_the_snapshot(
    client: TestClient, session: Session, mail: dict[str, uuid.UUID]
) -> None:
    client.post(f"/labeling/emails/{mail['hw']}/label", json=body(hw_item()))
    msg = session.get(Message, mail["hw"])
    assert msg is not None
    msg.subject = "Changed after labeling"
    session.flush()
    data = client.get(f"/labeling/emails/{mail['hw']}").json()
    assert data["frozen"] is True
    assert data["subject"] == "CS 000: Homework 4"
    assert data["eval_set"] == "dev"
    assert data["labels"]["items"][0]["title"] == "Homework 4"


def test_save_into_the_drawn_set(
    client: TestClient, session: Session, mail: dict[str, uuid.UUID]
) -> None:
    # A later dev draw and the test draw; the email lands where its draw put it
    pick(session, mail["other"], "test")
    session.commit()
    resp = client.post(f"/labeling/emails/{mail['other']}/label", json=body())
    assert resp.status_code == 200
    data = resp.json()
    assert data["counts"] == counts(test=1, test_total=1, remaining=3)
    row = session.get(EvalLabel, uuid.UUID(data["label_id"]))
    assert row is not None
    assert (row.eval_set, row.set_version) == ("test", 1)


def test_the_request_cannot_choose_the_set(
    client: TestClient, session: Session, mail: dict[str, uuid.UUID]
) -> None:
    resp = client.post(f"/labeling/emails/{mail['hw']}/label", json=body(eval_set="test"))
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["field"] == "eval_set"
    count = session.execute(sa.select(sa.func.count()).select_from(EvalLabel)).scalar_one()
    assert count == 0


def test_unsampled_email_is_unknown(
    client: TestClient, session: Session, mail: dict[str, uuid.UUID]
) -> None:
    other = mail["other"]
    assert client.get(f"/labeling/emails/{other}").status_code == 404
    assert client.post(f"/labeling/emails/{other}/label", json=body()).status_code == 404
    assert client.post(f"/labeling/emails/{other}/skip").status_code == 404
    assert session.execute(sa.select(sa.func.count()).select_from(EvalLabel)).scalar_one() == 0
    assert audits(session) == []


def test_queue_groups_by_set(
    client: TestClient, session: Session, mail: dict[str, uuid.UUID]
) -> None:
    # The test set comes after the whole dev set, even with older mail
    pick(session, mail["other"], "test")
    session.commit()
    data = client.get("/labeling").json()
    assert data["counts"] == counts(test_total=1, remaining=4)
    last_dev = client.get(f"/labeling/emails/{mail['ext']}").json()
    assert last_dev["next_id"] == str(mail["other"])
    view = client.get(f"/labeling/emails/{mail['other']}").json()
    assert view["eval_set"] == "test"
    assert view["prev_id"] == str(mail["ext"])
    assert view["next_id"] is None


def test_only_the_newest_draw_is_queued(
    client: TestClient, session: Session, mail: dict[str, uuid.UUID]
) -> None:
    # A dev redraw (set version 2) replaces the dev draw in the queue
    pick(session, mail["other"], "dev", version=2)
    session.commit()
    data = client.get("/labeling").json()
    assert data["counts"] == counts(dev_total=1, remaining=1)
    assert data["first_id"] == str(mail["other"])
    assert client.get(f"/labeling/emails/{mail['hw']}").status_code == 404
    saved = client.post(f"/labeling/emails/{mail['other']}/label", json=body()).json()
    row = session.get(EvalLabel, uuid.UUID(saved["label_id"]))
    assert row is not None
    assert (row.eval_set, row.set_version) == ("dev", 2)


def test_no_draw_means_an_empty_queue(client: TestClient, session: Session) -> None:
    data = client.get("/labeling").json()
    assert data["counts"] == counts(dev_total=0)
    assert data["next_open_id"] is None
    assert data["first_id"] is None


def test_save_twice_conflicts(client: TestClient, mail: dict[str, uuid.UUID]) -> None:
    url = f"/labeling/emails/{mail['hw']}/label"
    assert client.post(url, json=body(hw_item())).status_code == 200
    assert client.post(url, json=body(hw_item(ref="other"))).status_code == 409


@pytest.mark.parametrize(
    ("data", "field"),
    [
        (body(hw_item(due_at=None)), "labels.items.0"),
        (body(hw_item(kind="event")), "labels.items.0.kind"),
        (body(hw_item(), tags=["Bad Tag"]), "tags.0"),
        (body(hw_item(), extra=MARKER), "extra"),
        (body(hw_item(due_raw="Saturday at noon")), "labels.items.0.due_raw"),
    ],
)
def test_bad_label_lists_fields_only(
    client: TestClient,
    session: Session,
    mail: dict[str, uuid.UUID],
    data: dict[str, Any],
    field: str,
) -> None:
    resp = client.post(f"/labeling/emails/{mail['hw']}/label", json=data)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert field in [d["field"] for d in detail]
    assert set(detail[0]) == {"field", "message"}
    text = resp.text
    assert MARKER not in text
    assert "Saturday" not in text
    assert "Bad Tag" not in text
    count = session.execute(sa.select(sa.func.count()).select_from(EvalLabel)).scalar_one()
    assert count == 0


def test_bad_snapshot_lists_fields_only(
    client: TestClient, session: Session, mail: dict[str, uuid.UUID]
) -> None:
    msg = session.get(Message, mail["news"])
    assert msg is not None
    msg.from_addr = f"not an address {MARKER}"
    session.commit()
    view = client.get(f"/labeling/emails/{mail['news']}").json()
    assert [p["field"] for p in view["problems"]] == ["email.from_addr"]
    resp = client.post(f"/labeling/emails/{mail['news']}/label", json=body())
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["field"] == "email.from_addr"
    assert MARKER not in resp.text


def test_changes_must_name_an_earlier_item(client: TestClient, mail: dict[str, uuid.UUID]) -> None:
    ext = hw_item(
        ref="cs000-hw4-v2",
        due_raw="Monday Nov 23 at 11:59pm PT",
        due_at="2026-11-24T07:59:00Z",
        changes="cs000-hw4",
    )
    url = f"/labeling/emails/{mail['ext']}/label"
    # The item it changes is not labeled yet
    resp = client.post(url, json=body(ext))
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["field"] == "labels.items.0.changes"
    client.post(f"/labeling/emails/{mail['hw']}/label", json=body(hw_item()))
    view = client.get(f"/labeling/emails/{mail['ext']}").json()
    assert view["refs"] == [{"ref": "cs000-hw4", "title": "Homework 4", "eval_set": "dev"}]
    assert client.post(url, json=body(ext)).status_code == 200


def test_refs_are_unique(client: TestClient, mail: dict[str, uuid.UUID]) -> None:
    client.post(f"/labeling/emails/{mail['hw']}/label", json=body(hw_item()))
    todo = hw_item(kind="todo", due_raw=None, due_at=None, title="Read the notes")
    resp = client.post(f"/labeling/emails/{mail['news']}/label", json=body(todo))
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["field"] == "labels.items.0.ref"


# ---- Correct ----


def test_correct_retires_and_replaces(
    client: TestClient, session: Session, mail: dict[str, uuid.UUID]
) -> None:
    saved = client.post(f"/labeling/emails/{mail['hw']}/label", json=body(hw_item()))
    old_id = saved.json()["label_id"]
    fixed = body(hw_item(title="Homework 4 written part"), tags=["course", "fixed"])
    resp = client.post(f"/labeling/labels/{old_id}/correct", json=fixed)
    assert resp.status_code == 200
    new_id = resp.json()["label_id"]
    assert new_id != old_id
    old = session.get(EvalLabel, uuid.UUID(old_id))
    new = session.get(EvalLabel, uuid.UUID(new_id))
    assert old is not None and new is not None
    session.refresh(old)
    assert old.retired_at is not None
    assert new.corrects == old.id
    assert new.snapshot == old.snapshot
    assert new.eval_set == "dev"
    assert new.tags == ["course", "fixed"]
    assert new.labels["items"][0]["title"] == "Homework 4 written part"
    # The item keeps its own ref: the retired row does not clash with it
    assert resp.json()["counts"]["dev"] == 1
    view = client.get(f"/labeling/emails/{mail['hw']}").json()
    assert view["label_id"] == new_id
    events = audits(session)
    assert [e.event_type for e in events] == ["eval_label.add", "eval_label.correct"]
    assert events[1].data == {"corrects": old_id, "eval_set": "dev"}
    no_content(events)
    assert store.load_set(session, "dev")[0].labels.items[0].title == "Homework 4 written part"


def test_correct_a_retired_label_conflicts(client: TestClient, mail: dict[str, uuid.UUID]) -> None:
    saved = client.post(f"/labeling/emails/{mail['hw']}/label", json=body(hw_item()))
    old_id = saved.json()["label_id"]
    fixed = {"labels": body(hw_item())["labels"]}
    assert client.post(f"/labeling/labels/{old_id}/correct", json=fixed).status_code == 200
    assert client.post(f"/labeling/labels/{old_id}/correct", json=fixed).status_code == 409


def test_bad_correction_keeps_the_old_label(
    client: TestClient, session: Session, mail: dict[str, uuid.UUID]
) -> None:
    saved = client.post(f"/labeling/emails/{mail['hw']}/label", json=body(hw_item()))
    old_id = saved.json()["label_id"]
    bad = {"labels": body(hw_item(due_at="2026-11-21 07:59"))["labels"]}
    resp = client.post(f"/labeling/labels/{old_id}/correct", json=bad)
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["field"] == "labels.items.0.due_at"
    old = session.get(EvalLabel, uuid.UUID(old_id))
    assert old is not None
    session.refresh(old)
    assert old.retired_at is None


# ---- Skip ----


def test_skip_leaves_the_queue(
    client: TestClient, session: Session, mail: dict[str, uuid.UUID]
) -> None:
    resp = client.post(f"/labeling/emails/{mail['hw']}/skip")
    assert resp.status_code == 200
    data = resp.json()
    assert data["counts"] == counts(skipped=1, remaining=2)
    assert data["next_open_id"] == str(mail["news"])
    # Skipping again records nothing new
    assert client.post(f"/labeling/emails/{mail['hw']}/skip").status_code == 200
    (event,) = audits(session)
    assert event.event_type == "eval_label.skip"
    assert event.subject_type == "message"
    assert event.subject_id == mail["hw"]
    assert event.data == {}
    assert client.get("/labeling").json()["next_open_id"] == str(mail["news"])
    view = client.get(f"/labeling/emails/{mail['hw']}").json()
    assert view["skipped"] is True


def test_skipped_email_can_still_be_labeled(client: TestClient, mail: dict[str, uuid.UUID]) -> None:
    client.post(f"/labeling/emails/{mail['hw']}/skip")
    resp = client.post(f"/labeling/emails/{mail['hw']}/label", json=body(hw_item()))
    assert resp.status_code == 200
    assert resp.json()["counts"] == counts(dev=1, remaining=2)


def test_skip_a_labeled_email_conflicts(client: TestClient, mail: dict[str, uuid.UUID]) -> None:
    client.post(f"/labeling/emails/{mail['hw']}/label", json=body(hw_item()))
    assert client.post(f"/labeling/emails/{mail['hw']}/skip").status_code == 409


def test_next_open_wraps_to_the_oldest(client: TestClient, mail: dict[str, uuid.UUID]) -> None:
    client.post(f"/labeling/emails/{mail['news']}/skip")
    resp = client.post(f"/labeling/emails/{mail['ext']}/skip")
    assert resp.json()["next_open_id"] == str(mail["hw"])
    last = client.post(f"/labeling/emails/{mail['hw']}/skip").json()
    assert last["next_open_id"] is None
    assert last["counts"]["remaining"] == 0
