"""Create the proposals table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-11

A proposal is a change waiting for the owner's decision (ADR 0009, decisions
2 and 6). M2 has one kind, `item_update`; `grants` and `actions` come in M3.

Proposals are never deleted, and once made their content is fixed: a trigger
rejects DELETE and TRUNCATE, rejects an UPDATE that changes anything except
`status`, `decided_at` and `updated_at`, and allows only these status moves:

    pending  -> approved, rejected, expired, superseded
    approved -> executed

Constraint names follow the naming convention in app/db/base.py.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GUARD_FN = """
CREATE FUNCTION sinan_proposal_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    fixed_old jsonb;
    fixed_new jsonb;
BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RAISE EXCEPTION '% on proposals is not allowed: proposals are kept', TG_OP
            USING ERRCODE = 'restrict_violation';
    END IF;
    fixed_old := to_jsonb(OLD) - 'status' - 'decided_at' - 'updated_at';
    fixed_new := to_jsonb(NEW) - 'status' - 'decided_at' - 'updated_at';
    IF fixed_new IS DISTINCT FROM fixed_old THEN
        RAISE EXCEPTION 'only status and decided_at of a proposal can change'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.status = OLD.status THEN
        IF NEW.decided_at IS DISTINCT FROM OLD.decided_at THEN
            RAISE EXCEPTION 'decided_at changes only with the status'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status = 'pending'
        AND NEW.status IN ('approved', 'rejected', 'expired', 'superseded') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'approved' AND NEW.status = 'executed' THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'proposal status cannot move from % to %', OLD.status, NEW.status
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""


def upgrade() -> None:
    op.create_table(
        "proposals",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        # The item_versions.version the change was based on
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("extraction_run_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('item_update')", name="ck_proposals_kind"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'superseded', 'executed')",
            name="ck_proposals_status",
        ),
        sa.CheckConstraint("base_version >= 1", name="ck_proposals_base_version"),
        sa.CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'",
            name="ck_proposals_payload_hash",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_proposals_payload_object",
        ),
        sa.CheckConstraint(
            "(decided_at IS NOT NULL) = (status IN ('approved', 'rejected', 'executed'))",
            name="ck_proposals_decided",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name="fk_proposals_item_id_items",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name="fk_proposals_message_id_messages",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_run_id"],
            ["extraction_runs.id"],
            name="fk_proposals_extraction_run_id_extraction_runs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_proposals"),
    )
    # Superseding looks up the proposals on an item
    op.create_index("ix_proposals_item_id_status", "proposals", ["item_id", "status"])
    # The expiry check finds pending proposals past their expiry time
    op.create_index("ix_proposals_status_expires_at", "proposals", ["status", "expires_at"])

    op.execute(GUARD_FN)
    op.execute(
        "CREATE TRIGGER proposals_guard BEFORE UPDATE OR DELETE ON proposals "
        "FOR EACH ROW EXECUTE FUNCTION sinan_proposal_guard()"
    )
    op.execute(
        "CREATE TRIGGER proposals_no_truncate BEFORE TRUNCATE ON proposals "
        "FOR EACH STATEMENT EXECUTE FUNCTION sinan_reject_change()"
    )


def downgrade() -> None:
    # Dropping the table also drops its triggers
    op.drop_table("proposals")
    op.execute("DROP FUNCTION sinan_proposal_guard()")
