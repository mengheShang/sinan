"""Create the M1 tables.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-05

Creates 8 of the 11 phase-one tables (ADR 0002). `item_versions` and
`audit_events` are append-only: a trigger rejects UPDATE, DELETE and TRUNCATE.
Constraint names follow the naming convention in app/db/base.py.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("item_versions", "audit_events")

REJECT_FN = """
CREATE FUNCTION sinan_reject_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% on % is not allowed: table is append-only', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""


def uuid_pk() -> sa.Column[Any]:
    return sa.Column(
        "id",
        sa.Uuid(),
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def ts(name: str, nullable: bool = False, now: bool = False) -> sa.Column[Any]:
    default = sa.text("now()") if now else None
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=default,
        nullable=nullable,
    )


def jsonb(name: str, default: str) -> sa.Column[Any]:
    """A NOT NULL JSONB column with a server default."""
    return sa.Column(
        name,
        postgresql.JSONB(),
        server_default=sa.text(default),
        nullable=False,
    )


def pk(table: str, column: str = "id") -> sa.PrimaryKeyConstraint:
    return sa.PrimaryKeyConstraint(column, name=f"pk_{table}")


def uq(table: str, *columns: str) -> sa.UniqueConstraint:
    joined = "_".join(columns)
    return sa.UniqueConstraint(*columns, name=f"uq_{table}_{joined}")


def ck(table: str, name: str, sql: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(sql, name=f"ck_{table}_{name}")


def fk(table: str, column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column],
        [f"{target}.id"],
        name=f"fk_{table}_{column}_{target}",
        ondelete=ondelete,
    )


def upgrade() -> None:
    op.create_table(
        "accounts",
        uuid_pk(),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        ts("created_at", now=True),
        ts("updated_at", now=True),
        ck("accounts", "provider", "provider IN ('gmail')"),
        ck("accounts", "role", "role IN ('school', 'personal')"),
        pk("accounts"),
        uq("accounts", "provider", "email"),
    )

    op.create_table(
        "credentials",
        uuid_pk(),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        ts("authorized_at"),
        ts("created_at", now=True),
        ts("updated_at", now=True),
        ck("credentials", "purpose", "purpose IN ('read', 'write')"),
        fk("credentials", "account_id", "accounts", "CASCADE"),
        pk("credentials"),
        uq("credentials", "account_id", "purpose"),
    )

    op.create_table(
        "sync_states",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("history_id", sa.Text(), nullable=True),
        ts("last_attempt_at", nullable=True),
        ts("last_success_at", nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "needs_reauth",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        ts("updated_at", now=True),
        fk("sync_states", "account_id", "accounts", "CASCADE"),
        pk("sync_states", "account_id"),
    )

    op.create_table(
        "messages",
        uuid_pk(),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("provider_msg_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=True),
        ts("sent_at", nullable=True),
        ts("received_at"),
        sa.Column("from_addr", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        jsonb("headers", "'{}'::jsonb"),
        jsonb("label_ids", "'[]'::jsonb"),
        sa.Column("body_text", sa.Text(), nullable=True),
        ts("created_at", now=True),
        fk("messages", "account_id", "accounts", "CASCADE"),
        pk("messages"),
        uq("messages", "account_id", "provider_msg_id"),
    )
    op.create_index(
        "ix_messages_account_id_received_at",
        "messages",
        ["account_id", "received_at"],
    )

    op.create_table(
        "extraction_runs",
        uuid_pk(),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("prompt_hash", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "item_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        ts("created_at", now=True),
        ck(
            "extraction_runs",
            "status",
            "status IN ('ok', 'invalid', 'error', 'skipped')",
        ),
        ck("extraction_runs", "prompt_hash", "prompt_hash ~ '^[0-9a-f]{64}$'"),
        fk("extraction_runs", "message_id", "messages", "CASCADE"),
        pk("extraction_runs"),
        uq("extraction_runs", "message_id", "prompt_version"),
    )

    op.create_table(
        "items",
        uuid_pk(),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("course", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("due_raw", sa.Text(), nullable=True),
        ts("due_at", nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'open'"),
            nullable=False,
        ),
        ts("created_at", now=True),
        ts("updated_at", now=True),
        ck("items", "kind", "kind IN ('deadline', 'todo')"),
        ck(
            "items",
            "status",
            "status IN ('open', 'done', 'dismissed', 'needs_review')",
        ),
        ck("items", "due_has_raw", "due_at IS NULL OR due_raw IS NOT NULL"),
        pk("items"),
    )
    op.create_index("ix_items_status_due_at", "items", ["status", "due_at"])

    op.create_table(
        "item_versions",
        uuid_pk(),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("change_type", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("before", postgresql.JSONB(), nullable=True),
        sa.Column("after", postgresql.JSONB(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("extraction_run_id", sa.Uuid(), nullable=True),
        ts("created_at", now=True),
        ck(
            "item_versions",
            "change_type",
            "change_type IN ('create', 'update', 'status')",
        ),
        ck("item_versions", "actor", "actor IN ('extraction', 'user')"),
        ck("item_versions", "version", "version >= 1"),
        ck(
            "item_versions",
            "extraction_has_evidence",
            "actor <> 'extraction' OR (message_id IS NOT NULL AND evidence IS NOT NULL)",
        ),
        fk("item_versions", "item_id", "items", "RESTRICT"),
        fk("item_versions", "message_id", "messages", "RESTRICT"),
        fk("item_versions", "extraction_run_id", "extraction_runs", "RESTRICT"),
        pk("item_versions"),
        uq("item_versions", "item_id", "version"),
    )

    op.create_table(
        "audit_events",
        uuid_pk(),
        ts("occurred_at", now=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("subject_type", sa.Text(), nullable=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        jsonb("data", "'{}'::jsonb"),
        pk("audit_events"),
    )
    op.create_index("ix_audit_events_occurred_at", "audit_events", ["occurred_at"])

    op.execute(REJECT_FN)
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_no_change BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION sinan_reject_change()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION sinan_reject_change()"
        )


def downgrade() -> None:
    # Dropping a table also drops its triggers
    op.drop_table("audit_events")
    op.drop_table("item_versions")
    op.drop_table("items")
    op.drop_table("extraction_runs")
    op.drop_table("messages")
    op.drop_table("sync_states")
    op.drop_table("credentials")
    op.drop_table("accounts")
    op.execute("DROP FUNCTION sinan_reject_change()")
