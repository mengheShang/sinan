"""New items and unparsed changes reach existing items (ADR 0023).

Synthetic mail, a FakeExtractor and a FakeLinkPicker only. Every course,
person and address is fictional. A match never changes an item by itself:
a moved date is only a pending proposal, and an unparsed one only a flag.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.actions.proposals import HashMismatch, approve, reject
from app.db.extract import Counts, extract_pending
from app.db.home import read_home
from app.db.models import Account, AuditEvent, Item, ItemVersion, Message, Proposal
from app.extraction.link_prompt import REPEAT_PROMPT_HASH, REPEAT_PROMPT_VERSION
from app.extraction.prefilter import Rules
from app.proposals import new_item_of, payload_hash
from app.providers.demo_data import gmail_auth
from app.providers.fakes import FakeExtractor, FakeLinkPicker

pytestmark = pytest.mark.db

LA = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 11, 2, 17, 0, tzinfo=UTC)
RUN = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
NOV20 = "Friday Nov 20 at 11:59pm PT"
NOV21 = "Saturday Nov 21 at 11:59pm PT"
OLD_DUE = datetime(2026, 11, 21, 7, 59, tzinfo=UTC)
NEW_DUE = datetime(2026, 11, 22, 7, 59, tzinfo=UTC)

POSTED = "Problem set 4 is due Friday Nov 20 at 11:59pm PT."
REMINDER = "Reminder: Problem set 4 is due Friday Nov 20 at 11:59pm PT."
NO_DATE = "Reminder: please start problem set 4 early."
MOVED = "Problem set 4 is now due Saturday Nov 21 at 11:59pm PT."
SIMILAR = "Please finish problem set 4 by Friday Nov 20 at 11:59pm PT."
DRAFT = "The problem set 4 draft is due Friday Nov 20 at 11:59pm PT."
UNPARSED = "Problem set 4 is now due next Friday."
UNPARSED_OTHER = "Midterm essay is now due next Friday."
NOTICE = "Problem set 4 office hours are on Friday Nov 20."


def entry(
    evidence: str,
    due_raw: str | None,
    change: str = "create",
    title: str = "Problem set 4",
    kind: str = "deadline",
) -> str:
    item = {
        "kind": kind,
        "course": "ECON 010",
        "title": title,
        "due_raw": due_raw,
        "evidence": evidence,
        "change_type": change,
    }
    return json.dumps({"items": [item]})


REPLIES = {
    REMINDER: entry(REMINDER, NOV20),
    NO_DATE: entry(NO_DATE, None, kind="todo"),
    MOVED: entry(MOVED, NOV21),
    SIMILAR: entry(SIMILAR, NOV20, title="Finish problem set 4"),
    DRAFT: entry(DRAFT, NOV20, title="Problem set 4 draft"),
    UNPARSED: entry(UNPARSED, "next Friday", "update"),
    UNPARSED_OTHER: entry(UNPARSED_OTHER, "next Friday", "update", "Midterm essay"),
    NOTICE: entry(NOTICE, None, title="Problem set 4 office hours", kind="notice"),
    # Last, since the reminder's text contains it
    POSTED: entry(POSTED, NOV20),
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


def mail(session: Session, body: str, hours: int, sender: str = "nakamura@example.edu") -> None:
    when = T0 + timedelta(hours=hours)
    msg = Message(
        account_id=account_id(session),
        provider_msg_id=uuid.uuid4().hex,
        received_at=when,
        sent_at=when,
        from_addr=sender,
        subject="ECON 010",
        headers={"Authentication-Results": gmail_auth(sender)},
        label_ids=["INBOX"],
        body_text=body,
    )
    session.add(msg)
    session.commit()


def extract(session: Session, picker: FakeLinkPicker | None = None) -> Counts:
    clock = lambda: RUN  # noqa: E731
    fake = FakeExtractor(REPLIES)
    return extract_pending(session, fake, Rules(), 50, 7, clock, picker=picker)


def items(session: Session) -> list[Item]:
    query = sa.select(Item).order_by(Item.created_at, Item.id)
    return list(session.execute(query).scalars())


def proposals(session: Session) -> list[Proposal]:
    return list(session.execute(sa.select(Proposal)).scalars())


def versions(session: Session, item_id: uuid.UUID) -> list[ItemVersion]:
    query = sa.select(ItemVersion).where(ItemVersion.item_id == item_id)
    return list(session.execute(query.order_by(ItemVersion.version)).scalars())


def posted(session: Session) -> Item:
    mail(session, POSTED, 0)
    extract(session)
    (item,) = items(session)
    return item


# ---- Decision 1: repeats ----


def test_a_word_for_word_repeat_saves_nothing(session: Session) -> None:
    item = posted(session)
    mail(session, REMINDER, 24)
    picker = FakeLinkPicker()
    counts = extract(session, picker)
    assert items(session) == [item]
    assert proposals(session) == []
    assert counts.repeats == 1
    # The same title needs no model call
    assert picker.calls == []


def test_a_repeat_without_a_due_time_saves_nothing(session: Session) -> None:
    item = posted(session)
    mail(session, NO_DATE, 24)
    counts = extract(session)
    assert items(session) == [item]
    assert counts.repeats == 1


def test_no_match_saves_a_new_item(session: Session) -> None:
    posted(session)
    mail(session, UNPARSED_OTHER.replace("is now", "is"), 24)
    replies_text = UNPARSED_OTHER.replace("is now", "is")
    REPLIES[replies_text] = entry(replies_text, None, title="Midterm essay", kind="todo")
    try:
        extract(session)
    finally:
        del REPLIES[replies_text]
    assert sorted(i.title for i in items(session)) == ["Midterm essay", "Problem set 4"]


def test_notices_are_never_matched(session: Session) -> None:
    posted(session)
    mail(session, NOTICE, 24)
    extract(session)
    assert sorted(i.kind for i in items(session)) == ["deadline", "notice"]


def test_untrusted_senders_are_never_matched(session: Session) -> None:
    posted(session)
    mail(session, REMINDER, 24, sender="promo@example.com")
    extract(session)
    found = items(session)
    assert len(found) == 2
    assert sorted(i.status for i in found) == ["needs_review", "open"]


# ---- Decision 1: a moved date becomes a proposal ----


def test_a_repeat_with_a_moved_date_is_a_proposal(session: Session) -> None:
    item = posted(session)
    mail(session, MOVED, 24)
    counts = extract(session)
    assert items(session) == [item]
    (proposal,) = proposals(session)
    assert proposal.item_id == item.id
    assert proposal.status == "pending"
    held = new_item_of(proposal.payload)
    assert held is not None
    assert held["title"] == "Problem set 4"
    assert held["model_change_type"] == "create"
    assert counts.proposals == 1
    session.refresh(item)
    assert item.due_at == OLD_DUE


def test_approving_moves_the_date(session: Session) -> None:
    item = posted(session)
    mail(session, MOVED, 24)
    extract(session)
    (proposal,) = proposals(session)
    approve(session, proposal.id, proposal.payload_hash, RUN)
    session.commit()
    session.refresh(item)
    assert item.due_at == NEW_DUE
    assert len(items(session)) == 1


def test_rejecting_saves_the_new_item(session: Session) -> None:
    """Decision 5: a wrong match must not lose the task."""
    item = posted(session)
    mail(session, MOVED, 24)
    extract(session)
    (proposal,) = proposals(session)
    saved = reject(session, proposal.id, RUN)
    session.commit()
    assert saved is not None
    found = items(session)
    assert {i.id for i in found} == {item.id, saved}
    new = session.get_one(Item, saved)
    assert (new.title, new.due_at, new.status) == ("Problem set 4", NEW_DUE, "open")
    session.refresh(item)
    assert item.due_at == OLD_DUE
    (first,) = versions(session, new.id)
    assert (first.change_type, first.actor) == ("create", "extraction")
    assert first.message_id == proposal.message_id
    assert first.evidence == MOVED
    event = session.execute(
        sa.select(AuditEvent).where(AuditEvent.event_type == "proposal.rejected")
    ).scalar_one()
    assert event.data["saved_item_id"] == str(saved)


def test_rejecting_an_update_saves_nothing(session: Session) -> None:
    item = posted(session)
    mail(session, MOVED, 24)
    REPLIES[MOVED] = entry(MOVED, NOV21, "update")
    try:
        extract(session)
    finally:
        REPLIES[MOVED] = entry(MOVED, NOV21)
    (proposal,) = proposals(session)
    assert new_item_of(proposal.payload) is None
    assert reject(session, proposal.id, RUN) is None
    session.commit()
    assert items(session) == [item]


def test_a_tampered_payload_is_refused_on_reject(session: Session) -> None:
    posted(session)
    mail(session, MOVED, 24)
    extract(session)
    (proposal,) = proposals(session)
    payload = dict(proposal.payload)
    payload["new_item"] = {**payload["new_item"], "title": "Something else"}
    # The trigger keeps the payload fixed, so the test bypasses it
    session.execute(sa.text("ALTER TABLE proposals DISABLE TRIGGER USER"))
    session.execute(sa.update(Proposal).where(Proposal.id == proposal.id).values(payload=payload))
    session.execute(sa.text("ALTER TABLE proposals ENABLE TRIGGER USER"))
    session.commit()
    with pytest.raises(HashMismatch):
        reject(session, proposal.id, RUN)
    session.commit()
    session.refresh(proposal)
    assert proposal.status == "pending"
    assert len(items(session)) == 1


def test_the_hash_covers_the_new_item(session: Session) -> None:
    posted(session)
    mail(session, MOVED, 24)
    extract(session)
    (proposal,) = proposals(session)
    assert payload_hash(proposal.payload) == proposal.payload_hash
    changed = {**proposal.payload, "new_item": {}}
    assert payload_hash(changed) != proposal.payload_hash


def test_an_older_email_changes_nothing(session: Session) -> None:
    item = posted(session)
    mail(session, MOVED, 48)
    extract(session)
    (proposal,) = proposals(session)
    approve(session, proposal.id, proposal.payload_hash, RUN)
    session.commit()
    # A reminder sent before the move arrives late
    mail(session, REMINDER, 24)
    counts = extract(session)
    assert counts.stale == 1
    assert len(proposals(session)) == 1
    assert items(session) == [item]


# ---- Decision 4: similar titles ask the model ----


def test_a_similar_title_asks_the_model(session: Session) -> None:
    item = posted(session)
    mail(session, SIMILAR, 24)
    picker = FakeLinkPicker({"<item>": '{"target": 1}'})
    counts = extract(session, picker)
    (asked,) = picker.calls
    assert "<item>" in asked and "<change>" not in asked
    assert items(session) == [item]
    assert counts.repeats == 1
    assert counts.link_picked == 1


def test_the_model_saying_no_saves_a_new_item(session: Session) -> None:
    posted(session)
    mail(session, DRAFT, 24)
    picker = FakeLinkPicker()
    extract(session, picker)
    assert len(picker.calls) == 1
    titles = sorted(i.title for i in items(session))
    assert titles == ["Problem set 4", "Problem set 4 draft"]


def test_without_a_picker_a_similar_title_is_new(session: Session) -> None:
    posted(session)
    mail(session, SIMILAR, 24)
    extract(session, None)
    assert len(items(session)) == 2


def test_a_model_link_with_a_moved_date_records_the_repeat_prompt(session: Session) -> None:
    posted(session)
    moved = SIMILAR.replace("Friday Nov 20", "Saturday Nov 21")
    REPLIES[moved] = entry(moved, NOV21, title="Finish problem set 4")
    try:
        mail(session, moved, 24)
        picker = FakeLinkPicker({"<item>": '{"target": 1}'})
        extract(session, picker)
    finally:
        del REPLIES[moved]
    (proposal,) = proposals(session)
    assert proposal.payload["link"] == "llm"
    event = session.execute(
        sa.select(AuditEvent).where(AuditEvent.event_type == "proposal.created")
    ).scalar_one()
    assert event.data["link_prompt_version"] == REPEAT_PROMPT_VERSION
    assert event.data["link_prompt_hash"] == REPEAT_PROMPT_HASH


# ---- Decision 2: unparsed changes (Issue #119) ----


def test_an_unparsed_change_flags_its_item(session: Session) -> None:
    item = posted(session)
    mail(session, UNPARSED, 24)
    counts = extract(session)
    assert items(session) == [item]
    assert proposals(session) == []
    assert counts.marked == 1
    session.refresh(item)
    assert item.status == "needs_review"
    assert item.due_at == OLD_DUE
    last = versions(session, item.id)[-1]
    assert (last.change_type, last.actor) == ("status", "extraction")
    assert last.after["review"] == "update_unparsed"
    assert last.after["new_due_raw"] == "next Friday"
    assert last.evidence == UNPARSED
    event = session.execute(
        sa.select(AuditEvent).where(AuditEvent.event_type == "item.status")
    ).scalar_one()
    assert event.data["review"] == "update_unparsed"


def test_an_unparsed_change_without_a_target_is_a_new_item(session: Session) -> None:
    posted(session)
    mail(session, UNPARSED_OTHER, 24)
    extract(session)
    assert len(items(session)) == 2
    essay = session.execute(sa.select(Item).where(Item.title == "Midterm essay")).scalar_one()
    assert essay.status == "needs_review"


def test_a_flagged_item_stays_on_the_home_page(session: Session) -> None:
    item = posted(session)
    mail(session, UNPARSED, 24)
    extract(session)
    # Five days after the old due time: past, but held by the flag
    later = OLD_DUE + timedelta(days=5)
    (entry_,) = read_home(session, later, LA).due
    assert entry_.view.id == item.id
    assert entry_.overdue
    assert entry_.view.review == "update_unparsed"
    assert entry_.view.evidence == UNPARSED
