"""ORM models.

M1 creates 8 of the 11 phase-one tables, plus `digests` and `digest_items`
(ADR 0008). M2 adds `proposals` and `eval_labels` (ADR 0009, decisions 2
and 3), and `eval_runs` (Issue #87). M3 adds `grants` and `actions` (ADR
0021, migration 0009).
Every model must be defined in or imported into this module so Alembic can
discover it.

All primary keys are UUIDs and all timestamps are stored in UTC.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.proposals import HASH_PATTERN

UTC_TS = DateTime(timezone=True)


def quoted(values: tuple[str, ...]) -> str:
    """SQL string literals for fixed values, comma-separated."""
    return ", ".join(f"'{v}'" for v in values)


def one_of(column: str, values: tuple[str, ...]) -> str:
    """SQL for a CHECK that limits a column to fixed values."""
    return f"{column} IN ({quoted(values)})"


ROLES = ("school", "personal")
# `calendar`: the calendar-only token (ADR 0021, decision 2)
PURPOSES = ("read", "write", "calendar")
RUN_STATUSES = ("ok", "invalid", "error", "skipped")
# `notice`: optional or general mail, shown once in the digest (0007)
ITEM_KINDS = ("deadline", "todo", "notice")
ITEM_STATUSES = ("open", "done", "dismissed", "needs_review")
CHANGE_TYPES = ("create", "update", "status")
# `system`: changes no person or extraction made, such as an expiry (0006)
VERSION_ACTORS = ("extraction", "user", "system")
DIGEST_SECTIONS = ("new", "due_soon", "needs_review")
PROPOSAL_KINDS = (
    "item_update",
    "calendar_create",
    "calendar_update",
    "mark_read",
    "unsubscribe",
)
# Which references each proposal kind needs (migration 0009)
PROPOSAL_SHAPE = """
(kind = 'item_update'
    AND item_id IS NOT NULL AND base_version IS NOT NULL
    AND message_id IS NOT NULL AND extraction_run_id IS NOT NULL)
OR (kind IN ('calendar_create', 'calendar_update')
    AND item_id IS NOT NULL AND base_version IS NOT NULL
    AND extraction_run_id IS NULL)
OR (kind IN ('mark_read', 'unsubscribe')
    AND message_id IS NOT NULL AND item_id IS NULL
    AND base_version IS NULL AND extraction_run_id IS NULL)
"""
GRANT_KINDS = ("mark_read", "label", "archive")
# Lowercase exact addresses, separated by single spaces once joined
GRANT_SENDERS = (
    "cardinality(senders) >= 1"
    " AND array_position(senders, NULL) IS NULL"
    " AND array_to_string(senders, ' ') = lower(array_to_string(senders, ' '))"
    " AND array_to_string(senders, ' ') ~ '^[^@ ]+@[^@ ]+( [^@ ]+@[^@ ]+)*$'"
)
MAX_CYCLE_CAP = 20
ACTION_KINDS = (
    "mark_read",
    "mark_unread",
    "label_add",
    "label_remove",
    "archive",
    "unarchive",
    "calendar_create",
    "calendar_update",
    "calendar_delete",
    "unsubscribe",
)
ACTION_STATUSES = (
    "pending",
    "running",
    "refused",
    "succeeded",
    "failed",
    "needs_confirmation",
)
# An undo has no proposal or grant; every other action has exactly one
ACTION_SOURCE = """
(undoes_action_id IS NULL AND num_nonnulls(proposal_id, grant_id) = 1)
OR (undoes_action_id IS NOT NULL AND proposal_id IS NULL AND grant_id IS NULL)
"""
PROPOSAL_STATUSES = ("pending", "approved", "rejected", "expired", "superseded", "executed")
# Statuses the owner decided; exactly these carry a decision time
DECIDED_STATUSES = ("approved", "rejected", "executed")
EVAL_SETS = ("dev", "test")
SNAPSHOT_KEYS = ("from_addr", "subject", "body_text", "sent_at", "role", "headers")
LABEL_KEYS = ("items", "safe_to_auto_read")


def has_keys(column: str, keys: tuple[str, ...]) -> str:
    """SQL for a CHECK that a JSONB column is an object with these keys."""
    quoted = ", ".join(f"'{k}'" for k in keys)
    return f"jsonb_typeof({column}) = 'object' AND {column} ?& ARRAY[{quoted}]"


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


def created_at() -> Mapped[datetime]:
    return mapped_column(UTC_TS, server_default=func.now())


def updated_at() -> Mapped[datetime]:
    return mapped_column(UTC_TS, server_default=func.now(), onupdate=func.now())


class Account(Base):
    """A connected mailbox."""

    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("provider", "email"),
        CheckConstraint("provider IN ('gmail')", name="provider"),
        CheckConstraint(one_of("role", ROLES), name="role"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class Credential(Base):
    """An encrypted OAuth refresh token. Never holds plaintext and is never logged."""

    __tablename__ = "credentials"
    __table_args__ = (
        UniqueConstraint("account_id", "purpose"),
        CheckConstraint(one_of("purpose", PURPOSES), name="purpose"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
    )
    purpose: Mapped[str] = mapped_column(Text)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    # Space-separated OAuth scopes that were granted
    scopes: Mapped[str] = mapped_column(Text)
    # When the user last authorized; used to warn before the 7-day expiry
    authorized_at: Mapped[datetime] = mapped_column(UTC_TS)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class SyncState(Base):
    """Sync cursor and health for one account."""

    __tablename__ = "sync_states"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Gmail historyId. An unsigned 64-bit number, so it is stored as text.
    history_id: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTC_TS)
    last_success_at: Mapped[datetime | None] = mapped_column(UTC_TS)
    # Error class or code only, never message content or tokens
    last_error: Mapped[str | None] = mapped_column(Text)
    needs_reauth: Mapped[bool] = mapped_column(server_default=text("false"))
    updated_at: Mapped[datetime] = updated_at()


class Message(Base):
    """Email metadata and body. One row per provider message, per account."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("account_id", "provider_msg_id"),
        Index(None, "account_id", "received_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
    )
    provider_msg_id: Mapped[str] = mapped_column(Text)
    thread_id: Mapped[str | None] = mapped_column(Text)
    # From the Date header; can be missing or wrong
    sent_at: Mapped[datetime | None] = mapped_column(UTC_TS)
    # When the provider received it (Gmail internalDate)
    received_at: Mapped[datetime] = mapped_column(UTC_TS)
    from_addr: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    # Only the headers Sinan needs, such as List-Unsubscribe
    headers: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        server_default=text("'{}'::jsonb"),
    )
    label_ids: Mapped[list[str]] = mapped_column(
        JSONB,
        server_default=text("'[]'::jsonb"),
    )
    body_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()


class ExtractionRun(Base):
    """One prefilter or LLM pass over one message with one prompt version."""

    __tablename__ = "extraction_runs"
    __table_args__ = (
        UniqueConstraint("message_id", "prompt_version"),
        CheckConstraint(one_of("status", RUN_STATUSES), name="status"),
        CheckConstraint("prompt_hash ~ '^[0-9a-f]{64}$'", name="prompt_hash"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"),
    )
    prompt_version: Mapped[str] = mapped_column(Text)
    # SHA-256 of the prompt template. The full prompt is never stored.
    prompt_hash: Mapped[str] = mapped_column(Text)
    # Empty when the prefilter skipped the message
    model: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    # Short reason or error code, never message content
    detail: Mapped[str | None] = mapped_column(Text)
    item_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at()


class Item(Base):
    """Current state of a deadline or to-do. History lives in item_versions."""

    __tablename__ = "items"
    __table_args__ = (
        CheckConstraint(one_of("kind", ITEM_KINDS), name="kind"),
        CheckConstraint(one_of("status", ITEM_STATUSES), name="status"),
        # A parsed time must come from raw text that can be shown as evidence
        CheckConstraint("due_at IS NULL OR due_raw IS NOT NULL", name="due_has_raw"),
        Index(None, "status", "due_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(Text)
    course: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    due_raw: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(UTC_TS)
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class ItemVersion(Base):
    """One change to an item. Append-only: a trigger rejects UPDATE and DELETE."""

    __tablename__ = "item_versions"
    __table_args__ = (
        UniqueConstraint("item_id", "version"),
        CheckConstraint(one_of("change_type", CHANGE_TYPES), name="change_type"),
        CheckConstraint(one_of("actor", VERSION_ACTORS), name="actor"),
        CheckConstraint("version >= 1", name="version"),
        # A change made by extraction must point at its email and evidence
        CheckConstraint(
            "actor <> 'extraction' OR (message_id IS NOT NULL AND evidence IS NOT NULL)",
            name="extraction_has_evidence",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"),
    )
    version: Mapped[int] = mapped_column(Integer)
    change_type: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    # Field values before and after the change
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Verbatim snippet from the email that supports the change
    evidence: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="RESTRICT"),
    )
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = created_at()


class AuditEvent(Base):
    """Log of every key action. Append-only: a trigger rejects UPDATE and DELETE.

    `account_id` and `subject_id` have no foreign keys, so the log outlives
    the rows it describes. `data` must never hold tokens or message content.
    """

    __tablename__ = "audit_events"
    __table_args__ = (Index(None, "occurred_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    occurred_at: Mapped[datetime] = mapped_column(UTC_TS, server_default=func.now())
    event_type: Mapped[str] = mapped_column(Text)
    # Which module acted: worker, api, executor or user
    actor: Mapped[str] = mapped_column(Text)
    account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    subject_type: Mapped[str | None] = mapped_column(Text)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        server_default=text("'{}'::jsonb"),
    )


class Digest(Base):
    """One daily digest, made by the Worker each morning (ADR 0008)."""

    __tablename__ = "digests"
    __table_args__ = (UniqueConstraint("digest_date"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    # The local day it is for, in SINAN_TIMEZONE
    digest_date: Mapped[date] = mapped_column(Date)
    generated_at: Mapped[datetime] = mapped_column(UTC_TS)
    # The previous digest's generated_at; items created after it are "new"
    since: Mapped[datetime | None] = mapped_column(UTC_TS)
    created_at: Mapped[datetime] = created_at()


class DigestItem(Base):
    """Which items were in which section of a digest. Copies no content."""

    __tablename__ = "digest_items"
    __table_args__ = (
        CheckConstraint(one_of("section", DIGEST_SECTIONS), name="section"),
        Index(None, "item_id"),
    )

    digest_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("digests.id", ondelete="CASCADE"),
        primary_key=True,
    )
    section: Mapped[str] = mapped_column(Text, primary_key=True)
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"),
        primary_key=True,
    )


class Proposal(Base):
    """A change or action waiting for the owner's decision (ADR 0009 and 0021).

    Never deleted, and only `status` and `decided_at` can change after
    insert: a trigger in migration 0004 enforces both, and the allowed status
    moves. `payload_hash` is app.proposals.payload_hash(payload).

    Which references a proposal needs depends on its kind (PROPOSAL_SHAPE):
    an `item_update` has all four; a calendar proposal has an item and no
    extraction run; `mark_read` and `unsubscribe` target an email and have
    no item. A one-click approval is inserted already `approved`.
    """

    __tablename__ = "proposals"
    __table_args__ = (
        CheckConstraint(one_of("kind", PROPOSAL_KINDS), name="kind"),
        CheckConstraint(PROPOSAL_SHAPE, name="shape"),
        CheckConstraint(one_of("status", PROPOSAL_STATUSES), name="status"),
        CheckConstraint("base_version >= 1", name="base_version"),
        CheckConstraint(f"payload_hash ~ '{HASH_PATTERN}'", name="payload_hash"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
        CheckConstraint(
            f"(decided_at IS NOT NULL) = ({one_of('status', DECIDED_STATUSES)})",
            name="decided",
        ),
        Index(None, "item_id", "status"),
        Index(None, "status", "expires_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(Text)
    # The item the change applies to; empty for an email action
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"),
    )
    # The item_versions.version the change was based on. A newer version of
    # the item supersedes the proposal (ADR 0009, decision 6). Not part of
    # the payload, so payload_hash does not cover it; the trigger keeps it
    # fixed instead. Empty when there is no item.
    base_version: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    # The email the change was detected in, or the email an action targets
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="RESTRICT"),
    )
    # The extraction run that found an item_update; empty for other kinds
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="RESTRICT"),
    )
    # SINAN_PROPOSAL_TTL_DAYS after creation, or the due time if sooner
    expires_at: Mapped[datetime] = mapped_column(UTC_TS)
    # When the owner approved or rejected it
    decided_at: Mapped[datetime | None] = mapped_column(UTC_TS)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class Grant(Base):
    """A standing permission the owner set in the Dashboard (ADR 0021, decision 5).

    Only the signed-in owner creates one; no model output or Worker path
    does (CLAUDE.md rule 5). Never deleted, and its scope never changes:
    after insert only `paused_at` and `revoked_at` move, and a revoke is
    final. A trigger in migration 0009 enforces this and sets `created_at`
    to the inserting transaction's time, so the 90-day limit holds.
    """

    __tablename__ = "grants"
    __table_args__ = (
        CheckConstraint(GRANT_SENDERS, name="senders"),
        CheckConstraint(
            f"cardinality(kinds) >= 1 AND kinds <@ ARRAY[{quoted(GRANT_KINDS)}]::text[]",
            name="kinds",
        ),
        CheckConstraint(f"cycle_cap BETWEEN 1 AND {MAX_CYCLE_CAP}", name="cycle_cap"),
        CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + interval '90 days'",
            name="expiry",
        ),
        Index(None, "account_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"),
    )
    # Exact, lowercase sender addresses; no domains
    senders: Mapped[list[str]] = mapped_column(ARRAY(Text))
    kinds: Mapped[list[str]] = mapped_column(ARRAY(Text))
    # Most actions per sync cycle; going over pauses the grant
    cycle_cap: Mapped[int] = mapped_column(Integer, server_default=text("20"))
    expires_at: Mapped[datetime] = mapped_column(UTC_TS)
    paused_at: Mapped[datetime | None] = mapped_column(UTC_TS)
    revoked_at: Mapped[datetime | None] = mapped_column(UTC_TS)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class Action(Base):
    """One external write, run by the Executor (ADR 0021; technical design).

    Its source is exactly one of an approved proposal or a grant, except an
    undo, which points at the action it reverts and has neither.
    `idempotency_key` is deterministic and unique, so one action takes
    effect once. Never deleted; after insert only its progress (status,
    attempts, timing, result, error) changes, and `undo_payload` is set at
    most once. A trigger in migration 0009 enforces this and the status
    moves. `result` and `error` hold codes, never message content.
    """

    __tablename__ = "actions"
    __table_args__ = (
        CheckConstraint(one_of("kind", ACTION_KINDS), name="kind"),
        CheckConstraint(one_of("status", ACTION_STATUSES), name="status"),
        CheckConstraint(ACTION_SOURCE, name="source"),
        CheckConstraint("undoes_action_id <> id", name="not_self"),
        CheckConstraint(f"idempotency_key ~ '{HASH_PATTERN}'", name="idempotency_key"),
        CheckConstraint("jsonb_typeof(target) = 'object'", name="target"),
        CheckConstraint("attempts >= 0", name="attempts"),
        CheckConstraint(
            "kind <> 'unsubscribe' OR undo_payload IS NULL",
            name="no_undo_unsubscribe",
        ),
        UniqueConstraint("idempotency_key"),
        UniqueConstraint("undoes_action_id"),
        Index(None, "status", "created_at"),
        Index(None, "proposal_id"),
        Index(None, "grant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(Text)
    # The account whose token the action uses
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"),
    )
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("proposals.id", ondelete="RESTRICT"),
    )
    grant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("grants.id", ondelete="RESTRICT"),
    )
    undoes_action_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actions.id", ondelete="RESTRICT"),
    )
    # The email or item the action is about, for tracing
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="RESTRICT"),
    )
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"),
    )
    # Provider IDs the action writes to, such as a Gmail message ID
    target: Mapped[dict[str, Any]] = mapped_column(JSONB)
    idempotency_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # What an undo needs, such as the labels before; never for unsubscribe
    undo_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UTC_TS)
    finished_at: Mapped[datetime | None] = mapped_column(UTC_TS)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class EvalLabel(Base):
    """One labeled email in the dev or test set (ADR 0009, decisions 3 and 5).

    `snapshot` and `labels` hold the `input` and `labels` objects of the
    format in evals/README.md. The evaluation reads this table, never
    `messages`, so a re-sync cannot change its inputs.

    Rows are never edited: the only UPDATE a trigger allows is retiring a row
    once, and a correction is a new row whose `corrects` points at the
    retired one. See app/db/labels.py.
    """

    __tablename__ = "eval_labels"
    __table_args__ = (
        UniqueConstraint("corrects"),
        CheckConstraint(one_of("eval_set", EVAL_SETS), name="eval_set"),
        CheckConstraint("set_version >= 1", name="set_version"),
        CheckConstraint(has_keys("snapshot", SNAPSHOT_KEYS), name="snapshot"),
        CheckConstraint(one_of("snapshot->>'role'", ROLES), name="snapshot_role"),
        CheckConstraint(has_keys("labels", LABEL_KEYS), name="labels"),
        CheckConstraint("jsonb_typeof(tags) = 'array'", name="tags"),
        CheckConstraint("corrects <> id", name="not_self"),
        # One current row per email, so an email is never in both sets at once
        Index(
            "ix_eval_labels_current_source",
            "source_message_id",
            unique=True,
            postgresql_where=text("retired_at IS NULL"),
        ),
        Index(None, "eval_set", "set_version"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    eval_set: Mapped[str] = mapped_column(Text)
    # Which sampling draw the row belongs to: bumped when a set is redrawn,
    # kept by a correction
    set_version: Mapped[int] = mapped_column(Integer)
    # messages.id of the email the snapshot was copied from. For reference
    # only: no foreign key, so the label outlives the message
    source_message_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB)
    tags: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # The retired row this one corrects
    corrects: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("eval_labels.id", ondelete="RESTRICT"),
    )
    # Set once, to the transaction time, when the row is retired
    retired_at: Mapped[datetime | None] = mapped_column(UTC_TS)
    # Always the inserting transaction's time; a trigger overwrites any value
    created_at: Mapped[datetime] = created_at()


class EvalRun(Base):
    """The raw model replies of one official test-set run (Issue #87).

    Kept in the local database only, never in the repository, so the
    evaluation can replay the run with `--recorded` and get the same
    numbers without calling the model. `replies` maps a sample id to the
    extraction reply, `link_replies` a found item's uid to the link step's
    reply, and `auth` a sample id to the `Authentication-Results` header read
    again from Gmail for the run. The replies hold text the model took from
    real mail, so nothing logs or prints them.

    Rows are never edited; a trigger rejects UPDATE and TRUNCATE. DELETE is
    allowed so the owner can purge them. The newest row of a set version is
    the official run. See evals/official.py.
    """

    __tablename__ = "eval_runs"
    __table_args__ = (
        CheckConstraint(one_of("eval_set", EVAL_SETS), name="eval_set"),
        CheckConstraint("set_version >= 1", name="set_version"),
        CheckConstraint(f"prompt_hash ~ '{HASH_PATTERN}'", name="prompt_hash"),
        CheckConstraint(f"link_prompt_hash ~ '{HASH_PATTERN}'", name="link_prompt_hash"),
        CheckConstraint(f"rules_hash ~ '{HASH_PATTERN}'", name="rules_hash"),
        CheckConstraint("jsonb_typeof(replies) = 'object'", name="replies"),
        CheckConstraint("jsonb_typeof(link_replies) = 'object'", name="link_replies"),
        CheckConstraint("jsonb_typeof(auth) = 'object'", name="auth"),
        Index(None, "eval_set", "set_version"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    eval_set: Mapped[str] = mapped_column(Text)
    set_version: Mapped[int] = mapped_column(Integer)
    # The labels were read as they stood at this time (labels.load_set as_of)
    labels_as_of: Mapped[datetime] = mapped_column(UTC_TS)
    model: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    prompt_hash: Mapped[str] = mapped_column(Text)
    link_prompt_version: Mapped[str] = mapped_column(Text)
    link_prompt_hash: Mapped[str] = mapped_column(Text)
    # SHA-256 of the effective prefilter and trust rules, never the rules
    rules_hash: Mapped[str] = mapped_column(Text)
    # Git commit of the code that made the run
    commit: Mapped[str] = mapped_column(Text)
    replies: Mapped[dict[str, str]] = mapped_column(JSONB)
    link_replies: Mapped[dict[str, str]] = mapped_column(JSONB)
    auth: Mapped[dict[str, str]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at()


# Why a due suggestion holds no due time (ADR 0022, decision 4)
SUGGESTION_REASONS = (
    # The model said the email gives no due time
    "none",
    # The reply was refused by validation
    "bad_json",
    "bad_schema",
    "quote_not_found",
    "invalid_date",
    "out_of_range",
    "nonexistent_time",
    "ambiguous_time",
    # The call failed for this item
    "error",
)


class DueSuggestion(Base):
    """The model's suggested due time for an item without one (ADR 0022, decision 4).

    One row per item, so an item is asked about at most once. A row with no
    `due_at` records why there is none, so the item is not asked again.
    The suggestion only prefills the owner's due form; nothing applies it.

    Append-only: a trigger rejects UPDATE, DELETE and TRUNCATE.
    """

    __tablename__ = "due_suggestions"
    __table_args__ = (
        CheckConstraint(one_of("reason", SUGGESTION_REASONS), name="reason"),
        CheckConstraint(f"prompt_hash ~ '{HASH_PATTERN}'", name="prompt_hash"),
        # A suggestion has its quote and no reason; no suggestion has a reason
        CheckConstraint(
            "(due_at IS NOT NULL AND quote IS NOT NULL AND reason IS NULL)"
            " OR (due_at IS NULL AND reason IS NOT NULL)",
            name="shape",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"), unique=True
    )
    # The email the model read: the one that created the item
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id", ondelete="RESTRICT"))
    due_at: Mapped[datetime | None] = mapped_column(UTC_TS)
    # The model gives a local date and time; the zone is always SINAN_TIMEZONE
    zone_assumed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # No time was given, so 23:59 was used
    time_assumed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Text from the email that supports the date, checked to appear in it
    quote: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    prompt_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()
