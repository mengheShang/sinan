"""The model's suggested due time through extraction, the Dashboard API and the
owner's confirm (ADR 0020), against a throwaway database.

All mail is synthetic and the model is a FakeExtractor, so no API key is used.
"""

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.db.extract import extract_pending
from app.db.item_due import BadLocalTime, NotReviewable, from_local, set_due
from app.db.models import Account, AuditEvent, Item, ItemVersion, Message, Proposal
from app.extraction.deadlines import HOME
from app.extraction.prefilter import Rules
from app.providers.demo_data import gmail_auth
from app.providers.fakes import FakeExtractor
from tests.test_digest import api_client

T0 = datetime(2026, 11, 10, 17, 0, tzinfo=UTC)
NO_RULES = Rules()
SENDER = "rivera@example.edu"

HW = "CS 000 Homework 9 is due Friday Nov 20 at 11:59pm PT."
LAB = "CS 000 Lab report is due the Monday after Thanksgiving at 5pm PT."
QUIZ = "CS 000 Quiz 2 is on 11/19 at 5pm PT."
# Friday Nov 20 at 11:59pm PT; Monday Nov 30 at 5pm PT; Nov 19 at 5pm PT
HW_DUE = datetime(2026, 11, 21, 7, 59, tzinfo=UTC)
LAB_DUE = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)
QUIZ_DUE = datetime(2026, 11, 20, 1, 0, tzinfo=UTC)


def entry(title: str, evidence: str, due_raw: str, due_utc: str | None) -> dict[str, Any]:
    return {
        "kind": "deadline",
        "course": "CS 000",
        "title": title,
        "due_raw": due_raw,
        "due_utc": due_utc,
        "evidence": evidence,
        "change_type": "create",
    }


def replies(hw_utc: str | None) -> dict[str, str]:
    hw = entry("Homework 9", HW, "Friday Nov 20 at 11:59pm PT", hw_utc)
    lab = entry("Lab report", LAB, "the Monday after Thanksgiving at 5pm PT", "2026-12-01T01:00:00Z")
    quiz = entry("Quiz 2", QUIZ, "11/19 at 5pm PT", "2026-11-20T01:00:00Z")
    return {
        "Homework 9 is due": json.dumps({"items": [hw]}),
        "Lab report is due": json.dumps({"items": [lab]}),
        "Quiz 2 is on": json.dumps({"items": [quiz]}),
    }


@pytest.fixture
def session(conn: Connection) -> Session:
    return Session(bind=conn, join_transaction_mode="create_savepoint")


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    yield from api_client(session, "demo")


def mail(session: Session, body: str, auth: str | None = None, hours: int = 0) -> None:
    owner = session.execute(sa.select(Account.id)).scalars().first()
    if owner is None:
        row = Account(provider="gmail", email="student@example.edu", role="school")
        session.add(row)
        session.flush()
        owner = row.id
    when = T0 + timedelta(hours=hours)
    msg = Message(
        account_id=owner,
        provider_msg_id=uuid.uuid4().hex,
        received_at=when,
        sent_at=when,
        from_addr=SENDER,
        subject="Fictional mail",
        headers={"Authentication-Results": auth or gmail_auth(SENDER)},
        label_ids=["INBOX"],
        body_text=body,
    )
    session.add(msg)
    session.commit()


def extract(session: Session, hw_utc: str | None = None) -> None:
    fake = FakeExtractor(replies(hw_utc))
    extract_pending(session, fake, NO_RULES, cap=50, clock=lambda: T0)


def item_of(session: Session, title: str) -> Item:
    return session.execute(sa.select(Item).where(Item.title == title)).scalar_one()


def first_after(session: Session, item_id: uuid.UUID) -> dict[str, Any]:
    query = sa.select(ItemVersion.after).where(
        ItemVersion.item_id == item_id, ItemVersion.version == 1
    )
    after: dict[str, Any] = session.execute(query).scalar_one()
    return after


def listed(client: TestClient, title: str) -> dict[str, Any]:
    found = client.get("/items").json()
    return next(i for i in found if i["title"] == title)


# ---- Extraction ----


def test_agreement_stays_open(session: Session) -> None:
    mail(session, HW)
    extract(session, "2026-11-21T07:59:00Z")
    hw = item_of(session, "Homework 9")
    assert (hw.status, hw.due_at) == ("open", HW_DUE)
    after = first_after(session, hw.id)
    assert after["due_check"] == "agree"
    assert after["due_suggested"] == HW_DUE.isoformat()


def test_disagreement_waits_with_the_parsed_time(session: Session) -> None:
    mail(session, HW)
    extract(session, "2026-11-28T07:59:00Z")
    hw = item_of(session, "Homework 9")
    assert (hw.status, hw.due_at) == ("needs_review", HW_DUE)
    after = first_after(session, hw.id)
    assert (after["review"], after["due_check"]) == ("due_disagrees", "disagree")


def test_parser_cannot_read_it_stores_the_suggestion(session: Session) -> None:
    mail(session, LAB)
    extract(session)
    lab = item_of(session, "Lab report")
    assert (lab.status, lab.due_at) == ("needs_review", None)
    after = first_after(session, lab.id)
    assert after["review"] == "relative_event"
    assert after["due_check"] == "suggested"
    assert after["due_suggested"] == LAB_DUE.isoformat()


def test_injected_suggestion_on_a_change_is_not_proposed(session: Session) -> None:
    mail(session, HW)
    extract(session, "2026-11-21T07:59:00Z")
    moved = "Update: CS 000 Homework 9 is now due Friday Dec 4 at 11:59pm PT."
    mail(session, moved, hours=1)
    change = entry("Homework 9", moved, "Friday Dec 4 at 11:59pm PT", "2026-12-31T23:59:00Z")
    change["change_type"] = "update"
    fake = FakeExtractor({"is now due": json.dumps({"items": [change]})})
    extract_pending(session, fake, NO_RULES, cap=50, clock=lambda: T0)
    assert session.execute(sa.select(Proposal)).first() is None
    statuses = sorted(i.status for i in session.execute(sa.select(Item)).scalars())
    assert statuses == ["needs_review", "open"]


# ---- API ----


def test_api_offers_the_suggestion_on_review_items_only(
    client: TestClient, session: Session
) -> None:
    for n, body in enumerate((HW, LAB, QUIZ)):
        mail(session, body, hours=n)
    extract(session, "2026-11-21T07:59:00Z")
    hw = listed(client, "Homework 9")
    assert (hw["suggested_at"], hw["can_set_due"]) == (None, False)
    lab = listed(client, "Lab report")
    assert lab["suggested_at"] == "2026-12-01T01:00:00Z"
    assert lab["due_at"] is None
    assert lab["can_set_due"] is True
    # A numeric date has two readings: the owner sets it, with no suggestion
    quiz = listed(client, "Quiz 2")
    assert (quiz["suggested_at"], quiz["can_set_due"]) == (None, True)


def test_confirm_the_suggestion_in_one_click(client: TestClient, session: Session) -> None:
    mail(session, LAB)
    extract(session)
    lab = listed(client, "Lab report")
    resp = client.post(f"/items/{lab['id']}/due", json={"due_at": lab["suggested_at"]})
    assert resp.status_code == 200
    assert resp.json() == {"due_at": "2026-12-01T01:00:00Z", "status": "open", "version": 2}
    row = item_of(session, "Lab report")
    assert (row.status, row.due_at) == ("open", LAB_DUE)
    version = session.execute(
        sa.select(ItemVersion).where(ItemVersion.item_id == row.id, ItemVersion.version == 2)
    ).scalar_one()
    assert (version.actor, version.change_type) == ("user", "update")
    assert version.after["due_source"] == "suggestion"
    assert version.after["review"] is None
    assert version.before == {"due_at": None, "status": "needs_review", "review": "relative_event"}
    event = session.execute(sa.select(AuditEvent).where(AuditEvent.event_type == "item.due"))
    assert event.scalar_one().data == {"version": 2, "source": "suggestion", "status": "open"}
    after = listed(client, "Lab report")
    assert (after["suggested_at"], after["review"], after["due_by_owner"]) == (None, None, True)
    # A second click finds it open
    again = client.post(f"/items/{lab['id']}/due", json={"due_at": lab["suggested_at"]})
    assert again.status_code == 409


def test_correct_with_a_wall_time(client: TestClient, session: Session) -> None:
    mail(session, LAB)
    extract(session)
    lab = listed(client, "Lab report")
    resp = client.post(f"/items/{lab['id']}/due", json={"local": "2026-11-30T12:00"})
    assert resp.status_code == 200
    assert resp.json()["due_at"] == "2026-11-30T20:00:00Z"
    version = session.execute(sa.select(ItemVersion).where(ItemVersion.version == 2))
    assert version.scalar_one().after["due_source"] == "owner"


def test_keep_the_parsed_time_after_a_disagreement(client: TestClient, session: Session) -> None:
    mail(session, HW)
    extract(session, "2026-11-28T07:59:00Z")
    hw = listed(client, "Homework 9")
    assert hw["suggested_at"] == "2026-11-28T07:59:00Z"
    resp = client.post(f"/items/{hw['id']}/due", json={"due_at": hw["due_at"]})
    assert resp.json()["status"] == "open"
    version = session.execute(sa.select(ItemVersion).where(ItemVersion.version == 2))
    assert version.scalar_one().after["due_source"] == "parser"


def test_untrusted_sender_stays_held_after_confirming(client: TestClient, session: Session) -> None:
    forged = "mx.google.com; dkim=none; spf=none; dmarc=fail (p=NONE) header.from=example.edu"
    mail(session, LAB, auth=forged)
    extract(session)
    lab = listed(client, "Lab report")
    assert lab["review"] == "untrusted_sender"
    assert lab["suggested_at"] == "2026-12-01T01:00:00Z"
    resp = client.post(f"/items/{lab['id']}/due", json={"due_at": lab["suggested_at"]})
    assert resp.json()["status"] == "needs_review"
    after = listed(client, "Lab report")
    assert (after["review"], after["due_at"]) == ("untrusted_sender", "2026-12-01T01:00:00Z")


def test_api_refuses_bad_bodies(client: TestClient, session: Session) -> None:
    mail(session, LAB)
    extract(session)
    lab = listed(client, "Lab report")
    url = f"/items/{lab['id']}/due"
    both = {"due_at": "2026-12-01T01:00:00Z", "local": "2026-11-30T17:00"}
    assert client.post(url, json=both).status_code == 422
    assert client.post(url, json={}).status_code == 422
    assert client.post(url, json={"due_at": "2026-12-01T01:00:00"}).status_code == 422
    # 1:30am happens twice on Nov 1 2026 in Los Angeles
    assert client.post(url, json={"local": "2026-11-01T01:30"}).status_code == 422
    unknown = f"/items/{uuid.uuid4()}/due"
    assert client.post(unknown, json={"due_at": "2026-12-01T01:00:00Z"}).status_code == 404


def test_open_item_cannot_be_set(session: Session) -> None:
    mail(session, HW)
    extract(session, "2026-11-21T07:59:00Z")
    hw = item_of(session, "Homework 9")
    with pytest.raises(NotReviewable):
        set_due(session, hw.id, LAB_DUE)


# ---- Pure ----


def test_from_local_refuses_gaps_and_overlaps() -> None:
    assert from_local(datetime(2026, 11, 30, 17, 0), HOME) == LAB_DUE
    with pytest.raises(BadLocalTime):
        from_local(datetime(2026, 11, 1, 1, 30), HOME)
    with pytest.raises(BadLocalTime):
        from_local(datetime(2027, 3, 14, 2, 30), HOME)
