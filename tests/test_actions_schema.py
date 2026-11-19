"""Schema tests for migration 0009: grants, actions, and the wider proposals.

These use the throwaway database fixtures from conftest.py. Every name and
value is fictional.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.db.models import (
    Account,
    Action,
    Credential,
    ExtractionRun,
    Grant,
    Item,
    Message,
    Proposal,
)
from app.proposals import payload_hash

pytestmark = pytest.mark.db

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
PAYLOAD = {"title": "Homework 3"}
SENDER = "news@example.com"


def insert(conn: Connection, model: Any, **values: Any) -> uuid.UUID:
    stmt = sa.insert(model).values(**values).returning(model.id)
    key: uuid.UUID = conn.execute(stmt).scalar_one()
    return key


def fails(conn: Connection, stmt: sa.Executable) -> None:
    """Assert the statement is rejected by a constraint or trigger."""
    savepoint = conn.begin_nested()
    with pytest.raises((sa.exc.IntegrityError, sa.exc.InternalError)):
        conn.execute(stmt)
    savepoint.rollback()


def key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def db_now(conn: Connection) -> datetime:
    found: datetime = conn.execute(sa.text("SELECT now()")).scalar_one()
    return found


def account(conn: Connection) -> uuid.UUID:
    email = f"student-{uuid.uuid4().hex[:8]}@example.edu"
    return insert(conn, Account, provider="gmail", email=email, role="personal")


def refs(conn: Connection) -> dict[str, uuid.UUID]:
    """An account, email, extraction run and item to point at."""
    acct = account(conn)
    msg = insert(
        conn,
        Message,
        account_id=acct,
        provider_msg_id=uuid.uuid4().hex,
        received_at=NOW,
        body_text="Fictional body.",
    )
    run = insert(
        conn,
        ExtractionRun,
        message_id=msg,
        prompt_version="v7",
        prompt_hash="0" * 64,
        status="ok",
    )
    item = insert(conn, Item, kind="deadline", title="Homework 3")
    return {"account": acct, "message": msg, "run": run, "item": item}


def proposal_values(kind: str, **refs_: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "payload": PAYLOAD,
        "payload_hash": payload_hash(PAYLOAD),
        "expires_at": NOW + timedelta(days=7),
        **refs_,
    }


# --- proposals ---------------------------------------------------------------


def test_calendar_proposal_needs_item_and_no_run(conn: Connection) -> None:
    r = refs(conn)
    good = proposal_values("calendar_create", item_id=r["item"], base_version=1)
    insert(conn, Proposal, **good)
    no_item = proposal_values("calendar_create", base_version=1)
    fails(conn, sa.insert(Proposal).values(**no_item))
    with_run = {**good, "message_id": r["message"], "extraction_run_id": r["run"]}
    fails(conn, sa.insert(Proposal).values(**with_run))


def test_email_proposal_needs_message_and_no_item(conn: Connection) -> None:
    r = refs(conn)
    for kind in ("mark_read", "unsubscribe"):
        insert(conn, Proposal, **proposal_values(kind, message_id=r["message"]))
        fails(conn, sa.insert(Proposal).values(**proposal_values(kind)))
        on_item = proposal_values(kind, message_id=r["message"], item_id=r["item"], base_version=1)
        fails(conn, sa.insert(Proposal).values(**on_item))


def test_item_update_still_needs_everything(conn: Connection) -> None:
    r = refs(conn)
    full = {
        "item_id": r["item"],
        "base_version": 1,
        "message_id": r["message"],
        "extraction_run_id": r["run"],
    }
    insert(conn, Proposal, **proposal_values("item_update", **full))
    for column in full:
        partial = {k: v for k, v in full.items() if k != column}
        fails(conn, sa.insert(Proposal).values(**proposal_values("item_update", **partial)))


def test_one_click_proposal_is_inserted_approved(conn: Connection) -> None:
    r = refs(conn)
    values = proposal_values("mark_read", message_id=r["message"])
    insert(conn, Proposal, **values, status="approved", decided_at=NOW)
    # The decision time must come with it
    fails(conn, sa.insert(Proposal).values(**values, status="approved"))


def test_unknown_proposal_kind_is_rejected(conn: Connection) -> None:
    r = refs(conn)
    values = proposal_values("send_email", message_id=r["message"])
    fails(conn, sa.insert(Proposal).values(**values))


# --- credentials -------------------------------------------------------------


def test_calendar_credential_purpose(conn: Connection) -> None:
    acct = account(conn)
    values = {"account_id": acct, "ciphertext": b"x", "scopes": "s", "authorized_at": NOW}
    insert(conn, Credential, purpose="calendar", **values)
    fails(conn, sa.insert(Credential).values(purpose="send", **values))


# --- grants ------------------------------------------------------------------


def grant_values(conn: Connection, **changes: Any) -> dict[str, Any]:
    row = {
        "account_id": account(conn),
        "senders": [SENDER],
        "kinds": ["mark_read"],
        "expires_at": db_now(conn) + timedelta(days=90),
    }
    row.update(changes)
    return row


def new_grant(conn: Connection, **changes: Any) -> uuid.UUID:
    return insert(conn, Grant, **grant_values(conn, **changes))


def update_grant(gid: uuid.UUID, **values: Any) -> sa.Update:
    return sa.update(Grant).where(Grant.id == gid).values(**values)


def test_grant_defaults(conn: Connection) -> None:
    gid = new_grant(conn)
    row = conn.execute(sa.select(Grant).where(Grant.id == gid)).one()
    assert row.cycle_cap == 20
    assert row.paused_at is None
    assert row.revoked_at is None


@pytest.mark.parametrize(
    "senders",
    [
        [],
        ["News@example.com"],
        ["example.com"],
        ["@example.com"],
        ["a@b@example.com"],
        ["two words@example.com"],
        [SENDER, ""],
    ],
)
def test_grant_senders_are_exact_lowercase_addresses(conn: Connection, senders: list[str]) -> None:
    fails(conn, sa.insert(Grant).values(**grant_values(conn, senders=senders)))


def test_grant_accepts_several_senders(conn: Connection) -> None:
    new_grant(conn, senders=[SENDER, "digest@example.org"])


@pytest.mark.parametrize("kinds", [[], ["send"], ["mark_read", "delete"], ["unsubscribe"]])
def test_grant_kinds_are_limited(conn: Connection, kinds: list[str]) -> None:
    fails(conn, sa.insert(Grant).values(**grant_values(conn, kinds=kinds)))


@pytest.mark.parametrize("cap", [0, 21])
def test_grant_cap_is_one_to_twenty(conn: Connection, cap: int) -> None:
    fails(conn, sa.insert(Grant).values(**grant_values(conn, cycle_cap=cap)))


def test_grant_expiry_is_at_most_90_days(conn: Connection) -> None:
    now = db_now(conn)
    late = now + timedelta(days=90, seconds=1)
    fails(conn, sa.insert(Grant).values(**grant_values(conn, expires_at=late)))
    fails(conn, sa.insert(Grant).values(**grant_values(conn, expires_at=now)))


def test_grant_cannot_be_backdated(conn: Connection) -> None:
    now = db_now(conn)
    values = grant_values(
        conn,
        created_at=now - timedelta(days=30),
        expires_at=now + timedelta(days=100),
    )
    fails(conn, sa.insert(Grant).values(**values))


def test_grant_scope_never_changes(conn: Connection) -> None:
    gid = new_grant(conn)
    fails(conn, update_grant(gid, senders=["other@example.com"]))
    fails(conn, update_grant(gid, kinds=["archive"]))
    fails(conn, update_grant(gid, expires_at=db_now(conn) + timedelta(days=1)))
    fails(conn, update_grant(gid, cycle_cap=5))


def test_grant_pauses_resumes_and_revokes_once(conn: Connection) -> None:
    gid = new_grant(conn)
    conn.execute(update_grant(gid, paused_at=NOW))
    conn.execute(update_grant(gid, paused_at=None))
    conn.execute(update_grant(gid, revoked_at=NOW))
    fails(conn, update_grant(gid, revoked_at=None))
    fails(conn, update_grant(gid, paused_at=NOW))


def test_grant_is_never_deleted(conn: Connection) -> None:
    gid = new_grant(conn)
    fails(conn, sa.delete(Grant).where(Grant.id == gid))


# --- actions -----------------------------------------------------------------


def approved_proposal(conn: Connection, r: dict[str, uuid.UUID]) -> uuid.UUID:
    values = proposal_values("mark_read", message_id=r["message"])
    return insert(conn, Proposal, **values, status="approved", decided_at=NOW)


def action_values(r: dict[str, uuid.UUID], **changes: Any) -> dict[str, Any]:
    row = {
        "kind": "mark_read",
        "account_id": r["account"],
        "message_id": r["message"],
        "target": {"provider_msg_id": "m1"},
        "idempotency_key": key(uuid.uuid4().hex),
    }
    row.update(changes)
    return row


def new_action(conn: Connection, **changes: Any) -> uuid.UUID:
    r = refs(conn)
    if "grant_id" not in changes and "undoes_action_id" not in changes:
        changes.setdefault("proposal_id", approved_proposal(conn, r))
    return insert(conn, Action, **action_values(r, **changes))


def update_action(aid: uuid.UUID, **values: Any) -> sa.Update:
    return sa.update(Action).where(Action.id == aid).values(**values)


def status_of(conn: Connection, aid: uuid.UUID) -> str:
    found: str = conn.execute(sa.select(Action.status).where(Action.id == aid)).scalar_one()
    return found


def test_action_from_proposal_or_grant(conn: Connection) -> None:
    aid = new_action(conn)
    assert status_of(conn, aid) == "pending"
    new_action(conn, grant_id=new_grant(conn))


def test_action_needs_exactly_one_source(conn: Connection) -> None:
    r = refs(conn)
    fails(conn, sa.insert(Action).values(**action_values(r)))
    both = action_values(r, proposal_id=approved_proposal(conn, r), grant_id=new_grant(conn))
    fails(conn, sa.insert(Action).values(**both))


def test_undo_has_no_proposal_or_grant(conn: Connection) -> None:
    original = new_action(conn)
    r = refs(conn)
    undo = action_values(r, kind="mark_unread", undoes_action_id=original)
    insert(conn, Action, **undo)
    with_grant = action_values(
        r, kind="mark_unread", undoes_action_id=original, grant_id=new_grant(conn)
    )
    fails(conn, sa.insert(Action).values(**with_grant))


def test_action_is_undone_at_most_once(conn: Connection) -> None:
    original = new_action(conn)
    new_action(conn, kind="mark_unread", undoes_action_id=original)
    r = refs(conn)
    again = action_values(r, kind="mark_unread", undoes_action_id=original)
    fails(conn, sa.insert(Action).values(**again))


def test_idempotency_key_is_unique_and_hex(conn: Connection) -> None:
    same = key("mark_read:m1")
    new_action(conn, idempotency_key=same)
    r = refs(conn)
    proposal = approved_proposal(conn, r)
    fails(
        conn,
        sa.insert(Action).values(**action_values(r, proposal_id=proposal, idempotency_key=same)),
    )
    for bad in ("not-a-key", "A" * 64, "0" * 63):
        values = action_values(r, proposal_id=proposal, idempotency_key=bad)
        fails(conn, sa.insert(Action).values(**values))


def test_unknown_action_kind_is_rejected(conn: Connection) -> None:
    r = refs(conn)
    values = action_values(r, kind="send", proposal_id=approved_proposal(conn, r))
    fails(conn, sa.insert(Action).values(**values))


def test_unsubscribe_has_no_undo_payload(conn: Connection) -> None:
    r = refs(conn)
    values = action_values(
        r,
        kind="unsubscribe",
        proposal_id=approved_proposal(conn, r),
        undo_payload={"labels": []},
    )
    fails(conn, sa.insert(Action).values(**values))


@pytest.mark.parametrize(
    "path",
    [
        ["running", "succeeded"],
        ["running", "failed"],
        ["running", "needs_confirmation", "succeeded"],
        ["running", "needs_confirmation", "failed"],
        ["running", "pending", "running", "succeeded"],
        ["refused"],
    ],
)
def test_allowed_status_moves(conn: Connection, path: list[str]) -> None:
    aid = new_action(conn)
    for status in path:
        conn.execute(update_action(aid, status=status))
    assert status_of(conn, aid) == path[-1]


@pytest.mark.parametrize(
    ("path", "bad"),
    [
        ([], "succeeded"),
        ([], "needs_confirmation"),
        (["refused"], "running"),
        (["running", "succeeded"], "running"),
        (["running", "failed"], "pending"),
        (["running", "needs_confirmation"], "running"),
    ],
)
def test_forbidden_status_moves(conn: Connection, path: list[str], bad: str) -> None:
    aid = new_action(conn)
    for status in path:
        conn.execute(update_action(aid, status=status))
    fails(conn, update_action(aid, status=bad))


def test_only_progress_changes(conn: Connection) -> None:
    aid = new_action(conn)
    progress = {
        "status": "running",
        "attempts": 1,
        "started_at": NOW,
        "result": {"code": "ok"},
        "error": None,
        "finished_at": NOW,
    }
    conn.execute(update_action(aid, **progress))
    fails(conn, update_action(aid, kind="archive"))
    fails(conn, update_action(aid, target={"provider_msg_id": "m2"}))
    fails(conn, update_action(aid, idempotency_key=key("other")))


def test_undo_payload_is_set_once(conn: Connection) -> None:
    aid = new_action(conn)
    conn.execute(update_action(aid, undo_payload={"labels": ["UNREAD"]}))
    fails(conn, update_action(aid, undo_payload={"labels": []}))


def test_action_is_never_deleted(conn: Connection) -> None:
    aid = new_action(conn)
    fails(conn, sa.delete(Action).where(Action.id == aid))
