"""Create `due_suggestions`: the model's suggested due time for an item.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-26

ADR 0022, decision 4, and Issue #115. For an active deadline or to-do with
no due time, the Worker asks the model once for a suggestion. The row keeps
what the model suggested and how, or why there is no suggestion, so the
item is never asked about again:

- `item_id` is unique: one row per item.
- `message_id` is the email the model read, the one that created the item.
- `due_at` is set only for a suggestion that passed validation; it then has
  its `quote` and no `reason`. Without one, `reason` says why.
- `model`, `prompt_version` and `prompt_hash` trace the call.

The suggestion only prefills the owner's due form. The version the owner's
save appends records the suggestion's ID (Issue #113).

Append-only, like `item_versions`: triggers reject UPDATE, DELETE and
TRUNCATE. Both foreign keys restrict deletes.

Downgrade drops the table and its rows. Suggestions can be asked for again,
but item versions that name a suggestion ID keep the bare ID.

Constraint names follow the naming convention in app/db/base.py.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HASH = "'^[0-9a-f]{64}$'"
REASONS = (
    "'none', 'bad_json', 'bad_schema', 'quote_not_found', 'invalid_date', "
    "'out_of_range', 'nonexistent_time', 'ambiguous_time', 'error'"
)
SHAPE = (
    "(due_at IS NOT NULL AND quote IS NOT NULL AND reason IS NULL)"
    " OR (due_at IS NULL AND reason IS NOT NULL)"
)


def upgrade() -> None:
    op.create_table(
        "due_suggestions",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("zone_assumed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("time_assumed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("prompt_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(f"reason IN ({REASONS})", name="ck_due_suggestions_reason"),
        sa.CheckConstraint(f"prompt_hash ~ {HASH}", name="ck_due_suggestions_prompt_hash"),
        sa.CheckConstraint(SHAPE, name="ck_due_suggestions_shape"),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name="fk_due_suggestions_item_id_items",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name="fk_due_suggestions_message_id_messages",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_due_suggestions"),
        sa.UniqueConstraint("item_id", name="uq_due_suggestions_item_id"),
    )
    op.execute(
        "CREATE TRIGGER due_suggestions_no_change BEFORE UPDATE OR DELETE ON due_suggestions "
        "FOR EACH ROW EXECUTE FUNCTION sinan_reject_change()"
    )
    op.execute(
        "CREATE TRIGGER due_suggestions_no_truncate BEFORE TRUNCATE ON due_suggestions "
        "FOR EACH STATEMENT EXECUTE FUNCTION sinan_reject_change()"
    )


def downgrade() -> None:
    # Dropping the table also drops its triggers
    op.drop_table("due_suggestions")
