"""Sender trust (ADR 0010) through extraction, against a throwaway database.

All mail is synthetic and the model is a FakeExtractor, so no API key is used.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.db.extract import extract_pending
from app.db.models import Account, AuditEvent, Item, ItemVersion, Message, Proposal
from app.extraction.prefilter import Rules
from app.providers.demo_data import gmail_auth
from app.providers.fakes import FakeExtractor

T0 = datetime(2026, 11, 10, 17, 0, tzinfo=UTC)
NO_RULES = Rules()

FORGED = (
    "mx.google.com; dkim=none (no signature); spf=pass (google.com: domain of"
    " bounce@relay.example.net designates 192.0.2.40 as permitted sender)"
    " smtp.mailfrom=bounce@relay.example.net; dmarc=fail (p=NONE) header.from=example.edu"
)

HW = "CS 000 Homework 9 is due Friday Nov 20 at 11:59pm PT."
MOVED = "Update: CS 000 Homework 9 is now due Friday Dec 18 at 11:59pm PT."
TALK = "Optional talk: register by Friday Nov 20 at 5pm PT if you want to attend."


def item(kind: str, evidence: str, due_raw: str, change: str = "create") -> str:
    found = {
        "kind": kind,
        "course": "CS 000",
        "title": "Homework 9" if kind != "notice" else "Optional talk",
        "evidence": evidence,
        "due_raw": due_raw,
        "due_utc": None,
        "change_type": change,
    }
    return json.dumps({"items": [found]})


REPLIES = {
    "Homework 9 is due": item("deadline", HW, "Friday Nov 20 at 11:59pm PT"),
    "is now due": item("deadline", MOVED, "Friday Dec 18 at 11:59pm PT", "update"),
    "Optional talk": item("notice", TALK, "Friday Nov 20 at 5pm PT"),
}


@pytest.fixture
def session(conn: Connection) -> Session:
    return Session(bind=conn, join_transaction_mode="create_savepoint")


def account(session: Session, email: str, role: str = "school") -> uuid.UUID:
    row = Account(provider="gmail", email=email, role=role)
    session.add(row)
    session.commit()
    return row.id


def mail(
    session: Session, owner: uuid.UUID, sender: str, auth: str | None, body: str, hours: int
) -> None:
    when = T0 + timedelta(hours=hours)
    headers = {} if auth is None else {"Authentication-Results": auth}
    msg = Message(
        account_id=owner,
        provider_msg_id=uuid.uuid4().hex,
        received_at=when,
        sent_at=when,
        from_addr=sender,
        subject="Fictional mail",
        headers=headers,
        label_ids=["INBOX"],
        body_text=body,
    )
    session.add(msg)
    session.commit()


def extract(session: Session) -> None:
    extract_pending(session, FakeExtractor(REPLIES), NO_RULES, cap=50, clock=lambda: T0)


def items(session: Session) -> list[tuple[str, str]]:
    query = sa.select(Item).order_by(Item.created_at)
    return [(i.kind, i.status) for i in session.execute(query).scalars()]


def afters(session: Session) -> list[dict[str, object]]:
    query = sa.select(ItemVersion).order_by(ItemVersion.created_at)
    return [v.after for v in session.execute(query).scalars()]


def test_school_domain_with_dmarc_pass_stays_open(session: Session) -> None:
    owner = account(session, "student@example.edu")
    sender = "rivera@example.edu"
    mail(session, owner, sender, gmail_auth(sender), HW, 0)
    extract(session)
    assert items(session) == [("deadline", "open")]
    (after,) = afters(session)
    assert (after["review"], after["sender_trust"]) == (None, "trusted")


@pytest.mark.parametrize(
    ("sender", "auth", "reason"),
    [
        ("rivera@example.edu", FORGED, "auth_failed"),
        ("rivera@example.edu", None, "no_auth"),
        ("friend@webmail.example.com", gmail_auth("friend@webmail.example.com"), "unknown_sender"),
    ],
)
def test_untrusted_deadline_waits_for_review(
    session: Session, sender: str, auth: str | None, reason: str
) -> None:
    owner = account(session, "student@example.edu")
    mail(session, owner, sender, auth, HW, 0)
    extract(session)
    assert items(session) == [("deadline", "needs_review")]
    (after,) = afters(session)
    assert (after["review"], after["sender_trust"]) == ("untrusted_sender", reason)
    event = session.execute(sa.select(AuditEvent)).scalar_one()
    assert event.data["untrusted"] == 1


def test_personal_account_domain_is_not_trusted(session: Session) -> None:
    owner = account(session, "me@webmail.example.com", role="personal")
    sender = "friend@webmail.example.com"
    mail(session, owner, sender, gmail_auth(sender), HW, 0)
    extract(session)
    assert items(session) == [("deadline", "needs_review")]


def test_notice_from_untrusted_sender_is_unaffected(session: Session) -> None:
    owner = account(session, "student@example.edu")
    sender = "events@webmail.example.com"
    mail(session, owner, sender, gmail_auth(sender), TALK, 0)
    extract(session)
    assert items(session) == [("notice", "open")]
    (after,) = afters(session)
    assert after["sender_trust"] == "unknown_sender"


def test_forged_change_never_becomes_a_proposal(session: Session) -> None:
    owner = account(session, "student@example.edu")
    sender = "rivera@example.edu"
    mail(session, owner, sender, gmail_auth(sender), HW, 0)
    mail(session, owner, sender, FORGED, MOVED, 1)
    extract(session)
    assert session.execute(sa.select(sa.func.count()).select_from(Proposal)).scalar_one() == 0
    # The trusted item keeps its time; the change waits as its own item
    assert items(session) == [("deadline", "open"), ("deadline", "needs_review")]
    first = session.execute(sa.select(Item).order_by(Item.created_at)).scalars().first()
    assert first is not None
    assert first.due_at == datetime(2026, 11, 21, 7, 59, tzinfo=UTC)


def test_trusted_change_still_becomes_a_proposal(session: Session) -> None:
    owner = account(session, "student@example.edu")
    sender = "rivera@example.edu"
    mail(session, owner, sender, gmail_auth(sender), HW, 0)
    mail(session, owner, sender, gmail_auth(sender), MOVED, 1)
    extract(session)
    assert session.execute(sa.select(sa.func.count()).select_from(Proposal)).scalar_one() == 1
    assert items(session) == [("deadline", "open")]
