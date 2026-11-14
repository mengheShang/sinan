"""Prefilter and extraction against a throwaway database, with a recorded fake model.

All mail here is synthetic, and the model is a FakeExtractor or a stub, so
no test needs an API key.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.db.extract import extract_pending
from app.db.models import (
    Account,
    AuditEvent,
    ExtractionRun,
    Item,
    ItemVersion,
    Message,
    Proposal,
)
from app.extraction.prefilter import EXAMPLE_RULES, Rules, RuleSet, load_rules
from app.extraction.prompt import PROMPT_HASH, PROMPT_VERSION
from app.providers.demo_data import DEMO_MESSAGES, DEMO_RESPONSES
from app.providers.fakes import FakeExtractor
from app.providers.llm import ExtractorError

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
NO_RULES = Rules()


def make_session(conn: Connection) -> Session:
    return Session(bind=conn, join_transaction_mode="create_savepoint")


@pytest.fixture
def session(conn: Connection) -> Session:
    return make_session(conn)


def add_account(session: Session, role: str = "school") -> uuid.UUID:
    email = f"{uuid.uuid4().hex[:8]}@example.edu"
    stmt = sa.insert(Account).values(provider="gmail", email=email, role=role)
    account_id: uuid.UUID = session.execute(stmt.returning(Account.id)).scalar_one()
    session.commit()
    return account_id


def add_message(
    session: Session,
    account_id: uuid.UUID,
    body: str,
    subject: str = "Fictional notice",
    sender: str = "rivera@example.edu",
    minutes: int = 0,
) -> uuid.UUID:
    msg = Message(
        account_id=account_id,
        provider_msg_id=uuid.uuid4().hex,
        received_at=NOW + timedelta(minutes=minutes),
        sent_at=NOW + timedelta(minutes=minutes),
        from_addr=sender,
        subject=subject,
        headers={},
        label_ids=["INBOX"],
        body_text=body,
    )
    session.add(msg)
    session.commit()
    return msg.id


def runs(session: Session) -> list[ExtractionRun]:
    query = sa.select(ExtractionRun).order_by(ExtractionRun.created_at)
    return list(session.execute(query).scalars())


def count(session: Session, model: type) -> int:
    query = sa.select(sa.func.count()).select_from(model)
    return int(session.execute(query).scalar_one())


class FailingExtractor:
    model = "stub-model"

    def __init__(self, error: ExtractorError) -> None:
        self.error = error
        self.calls = 0

    def extract(self, prompt: str, text: str) -> str:
        self.calls += 1
        raise self.error


def test_demo_mailbox_end_to_end(session: Session) -> None:
    account = add_account(session)
    for n, demo in enumerate(DEMO_MESSAGES):
        add_message(session, account, demo.body_text or "", demo.subject or "", minutes=n)
    fake = FakeExtractor(DEMO_RESPONSES)
    counts = extract_pending(session, fake, load_rules(EXAMPLE_RULES), cap=100)
    assert counts.extracted == len(DEMO_MESSAGES)
    # The extension becomes a proposal for the first Homework 3, not an item
    assert (counts.items, counts.proposals) == (3, 1)
    items = list(session.execute(sa.select(Item).order_by(Item.title)).scalars())
    titles = [(i.title, i.status) for i in items]
    assert titles == [
        ("Homework 3", "open"),
        ("Resume workshop registration", "open"),
        ("Sign up for a project group", "open"),
    ]
    assert items[1].kind == "notice"
    assert items[1].due_at == datetime(2026, 11, 21, 1, 0, tzinfo=UTC)
    first = items[0]
    assert first.due_at == datetime(2026, 11, 14, 7, 59, tzinfo=UTC)
    proposal = session.execute(sa.select(Proposal)).scalar_one()
    assert (proposal.item_id, proposal.status, proposal.base_version) == (first.id, "pending", 1)


def test_item_version_points_at_message_run_and_evidence(session: Session) -> None:
    account = add_account(session)
    msg_id = add_message(session, account, DEMO_MESSAGES[0].body_text or "")
    extract_pending(session, FakeExtractor(DEMO_RESPONSES), NO_RULES, cap=10)
    (run,) = runs(session)
    assert (run.status, run.model, run.item_count) == ("ok", "fake-extractor", 1)
    assert (run.prompt_version, run.prompt_hash) == (PROMPT_VERSION, PROMPT_HASH)
    version = session.execute(sa.select(ItemVersion)).scalar_one()
    assert version.version == 1
    assert version.actor == "extraction"
    assert version.message_id == msg_id
    assert version.extraction_run_id == run.id
    assert version.evidence == "Homework 3 is due Friday Nov 13 at 11:59pm PT"
    assert version.after["due_at"] == "2026-11-14T07:59:00+00:00"
    assert version.after["review"] is None


def test_rerun_skips_messages_already_done(session: Session) -> None:
    account = add_account(session)
    add_message(session, account, DEMO_MESSAGES[0].body_text or "")
    fake = FakeExtractor(DEMO_RESPONSES)
    extract_pending(session, fake, NO_RULES, cap=10)
    again = extract_pending(session, fake, NO_RULES, cap=10)
    assert len(fake.calls) == 1
    assert again.extracted == 0
    assert count(session, Item) == 1


def test_new_prompt_version_reads_only_new_mail(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = add_account(session)
    add_message(session, account, DEMO_MESSAGES[0].body_text or "")
    add_message(session, account, "Big sale today.", sender="deals@shop.example.com", minutes=1)
    rules = Rules(skip=RuleSet(senders=frozenset({"deals@shop.example.com"})))
    fake = FakeExtractor(DEMO_RESPONSES)
    extract_pending(session, fake, rules, cap=10)
    # Both the extracted and the skipped message stay done under a new version
    monkeypatch.setattr("app.db.extract.PROMPT_VERSION", "v-next")
    again = extract_pending(session, fake, NO_RULES, cap=10)
    assert (again.extracted, again.skipped) == (0, 0)
    assert len(fake.calls) == 1
    assert count(session, Item) == 1
    assert len(runs(session)) == 2


def test_prefilter_skip_is_recorded_and_never_sent(session: Session) -> None:
    account = add_account(session)
    add_message(session, account, "Big sale today.", sender="deals@shop.example.com")
    rules = Rules(skip=RuleSet(senders=frozenset({"deals@shop.example.com"})))
    fake = FakeExtractor(DEMO_RESPONSES)
    counts = extract_pending(session, fake, rules, cap=10)
    assert fake.calls == []
    (run,) = runs(session)
    assert (run.status, run.detail, run.model) == ("skipped", "skip_sender", None)
    assert counts.reasons == {"skip_sender": 1}


def test_sensitive_rule_uses_the_account_role(session: Session) -> None:
    school = add_account(session, "school")
    personal = add_account(session, "personal")
    add_message(session, school, "Synthetic body.", subject="Updated roster")
    add_message(session, personal, "Synthetic body.", subject="Updated roster", minutes=1)
    rules = Rules(sensitive_keywords=("roster",))
    fake = FakeExtractor()
    extract_pending(session, fake, rules, cap=10)
    assert len(fake.calls) == 1
    statuses = sorted(r.status for r in runs(session))
    assert statuses == ["ok", "skipped"]


def test_injection_sample_produces_no_item(session: Session) -> None:
    account = add_account(session)
    injection = next(m for m in DEMO_MESSAGES if "ignore all previous" in (m.body_text or ""))
    add_message(session, account, injection.body_text or "")
    # A model talked into it: an action field the schema does not allow
    hijacked = FakeExtractor({"IMPORTANT": '{"items": [], "actions": ["archive_all"]}'})
    counts = extract_pending(session, hijacked, NO_RULES, cap=10)
    (run,) = runs(session)
    assert (run.status, run.detail) == ("invalid", "bad_schema")
    assert counts.items == 0
    assert count(session, Item) == 0


def test_evidence_not_in_body_produces_no_item(session: Session) -> None:
    account = add_account(session)
    add_message(session, account, "Office hours move to Room 101 this week.")
    reply = (
        '{"items": [{"kind": "deadline", "course": null, "title": "Quiz", '
        '"due_raw": "Nov 14 5pm PT", "evidence": "Quiz due Nov 14 5pm PT", '
        '"change_type": "create"}]}'
    )
    extract_pending(session, FakeExtractor({"Office hours": reply}), NO_RULES, cap=10)
    (run,) = runs(session)
    assert (run.status, run.detail, run.item_count) == ("ok", "dropped=1", 0)
    assert count(session, Item) == 0


def test_cap_limits_model_calls_but_not_skips(session: Session) -> None:
    account = add_account(session)
    for n in range(3):
        add_message(session, account, f"Synthetic body {n}.", minutes=n)
    add_message(session, account, "Sale.", sender="deals@shop.example.com", minutes=9)
    rules = Rules(skip=RuleSet(senders=frozenset({"deals@shop.example.com"})))
    fake = FakeExtractor()
    first = extract_pending(session, fake, rules, cap=2)
    assert (len(fake.calls), first.skipped) == (2, 1)
    second = extract_pending(session, fake, rules, cap=2)
    assert (len(fake.calls), second.extracted) == (3, 1)


def test_service_error_saves_nothing_and_stops(session: Session) -> None:
    account = add_account(session)
    add_message(session, account, "Synthetic body one.")
    add_message(session, account, "Synthetic body two.", minutes=1)
    stub = FailingExtractor(ExtractorError("rate_limited", "service"))
    counts = extract_pending(session, stub, NO_RULES, cap=10)
    assert stub.calls == 1
    assert counts.stopped == "rate_limited"
    assert runs(session) == []


def test_message_error_is_recorded_once(session: Session) -> None:
    account = add_account(session)
    add_message(session, account, "Synthetic body.")
    stub = FailingExtractor(ExtractorError("refusal", "message"))
    extract_pending(session, stub, NO_RULES, cap=10)
    extract_pending(session, stub, NO_RULES, cap=10)
    assert stub.calls == 1
    (run,) = runs(session)
    assert (run.status, run.detail, run.model) == ("error", "refusal", "stub-model")


def test_cycle_is_audited_without_content(session: Session) -> None:
    account = add_account(session)
    add_message(session, account, DEMO_MESSAGES[0].body_text or "")
    extract_pending(session, FakeExtractor(DEMO_RESPONSES), NO_RULES, cap=10)
    event = session.execute(sa.select(AuditEvent)).scalar_one()
    assert event.event_type == "extraction"
    assert event.data["extracted"] == 1
    assert "Homework" not in str(event.data)
