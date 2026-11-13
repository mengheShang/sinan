"""Create the evaluation label table.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-11

Each row is one labeled email in the development or test set (ADR 0009,
decisions 3 and 5): a frozen snapshot of exactly what the extractor and the
prefilter see, the labels, and the set it belongs to, in the format of
evals/README.md. The evaluation reads these rows, never `messages`.

A row is never edited in place. A trigger rejects DELETE and TRUNCATE, and
the only UPDATE it allows is retiring a row once: setting `retired_at`, which
it fills with the transaction time. An insert trigger likewise sets
`created_at` to the transaction time, ignoring any supplied value, so
neither time can be backdated. A correction retires the old row and
inserts a new one whose `corrects` points at it, with the same snapshot,
source message and set; the insert trigger checks that too.

Constraint names follow the naming convention in app/db/base.py.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GUARD_FN = """
CREATE FUNCTION sinan_eval_label_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RAISE EXCEPTION '% on eval_labels is not allowed: labels are kept', TG_OP
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.retired_at IS NOT NULL THEN
        RAISE EXCEPTION 'eval label % is already retired', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.retired_at IS NULL THEN
        RAISE EXCEPTION 'the only change to an eval label is retiring it'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (to_jsonb(NEW) - 'retired_at') IS DISTINCT FROM (to_jsonb(OLD) - 'retired_at') THEN
        RAISE EXCEPTION 'the only change to an eval label is retiring it'
            USING ERRCODE = 'restrict_violation';
    END IF;
    NEW.retired_at := now();
    RETURN NEW;
END;
$$;
"""

CORRECTION_FN = """
CREATE FUNCTION sinan_eval_label_correction() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    old_row eval_labels%ROWTYPE;
BEGIN
    -- Set here, not by the caller, so the label history cannot be backdated
    NEW.created_at := now();
    IF NEW.corrects IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT * INTO old_row FROM eval_labels WHERE id = NEW.corrects;
    IF NOT FOUND THEN
        RETURN NEW;  -- the foreign key reports it
    END IF;
    IF old_row.retired_at IS NULL THEN
        RAISE EXCEPTION 'retire eval label % before correcting it', old_row.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.snapshot IS DISTINCT FROM old_row.snapshot
        OR NEW.source_message_id IS DISTINCT FROM old_row.source_message_id
        OR NEW.eval_set IS DISTINCT FROM old_row.eval_set THEN
        RAISE EXCEPTION 'a correction keeps the snapshot, source message and set'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;
"""

SNAPSHOT_KEYS = "ARRAY['from_addr', 'subject', 'body_text', 'sent_at', 'role', 'headers']"
LABEL_KEYS = "ARRAY['items', 'safe_to_auto_read']"


def upgrade() -> None:
    op.create_table(
        "eval_labels",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("eval_set", sa.Text(), nullable=False),
        sa.Column("set_version", sa.Integer(), nullable=False),
        sa.Column("source_message_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("labels", postgresql.JSONB(), nullable=False),
        sa.Column(
            "tags",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("corrects", sa.Uuid(), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("eval_set IN ('dev', 'test')", name="ck_eval_labels_eval_set"),
        sa.CheckConstraint("set_version >= 1", name="ck_eval_labels_set_version"),
        sa.CheckConstraint(
            f"jsonb_typeof(snapshot) = 'object' AND snapshot ?& {SNAPSHOT_KEYS}",
            name="ck_eval_labels_snapshot",
        ),
        sa.CheckConstraint(
            "snapshot->>'role' IN ('school', 'personal')",
            name="ck_eval_labels_snapshot_role",
        ),
        sa.CheckConstraint(
            f"jsonb_typeof(labels) = 'object' AND labels ?& {LABEL_KEYS}",
            name="ck_eval_labels_labels",
        ),
        sa.CheckConstraint("jsonb_typeof(tags) = 'array'", name="ck_eval_labels_tags"),
        sa.CheckConstraint("corrects <> id", name="ck_eval_labels_not_self"),
        sa.ForeignKeyConstraint(
            ["corrects"],
            ["eval_labels.id"],
            name="fk_eval_labels_corrects_eval_labels",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_eval_labels"),
        # A row is corrected at most once, so corrections form a chain
        sa.UniqueConstraint("corrects", name="uq_eval_labels_corrects"),
    )
    # One current row per email, so an email is never in both sets at once
    op.create_index(
        "ix_eval_labels_current_source",
        "eval_labels",
        ["source_message_id"],
        unique=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )
    op.create_index(
        "ix_eval_labels_eval_set_set_version",
        "eval_labels",
        ["eval_set", "set_version"],
    )

    op.execute(GUARD_FN)
    op.execute(CORRECTION_FN)
    op.execute(
        "CREATE TRIGGER eval_labels_guard BEFORE UPDATE OR DELETE ON eval_labels "
        "FOR EACH ROW EXECUTE FUNCTION sinan_eval_label_guard()"
    )
    op.execute(
        "CREATE TRIGGER eval_labels_correction BEFORE INSERT ON eval_labels "
        "FOR EACH ROW EXECUTE FUNCTION sinan_eval_label_correction()"
    )
    op.execute(
        "CREATE TRIGGER eval_labels_no_truncate BEFORE TRUNCATE ON eval_labels "
        "FOR EACH STATEMENT EXECUTE FUNCTION sinan_reject_change()"
    )


def downgrade() -> None:
    # Dropping the table also drops its triggers
    op.drop_table("eval_labels")
    op.execute("DROP FUNCTION sinan_eval_label_correction()")
    op.execute("DROP FUNCTION sinan_eval_label_guard()")
