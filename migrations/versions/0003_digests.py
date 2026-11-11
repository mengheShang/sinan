"""Create the digest tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-11

The Worker writes one digest per local day (ADR 0008). `digest_items` lists
which items were in each section; it copies no content, so the Dashboard
joins to `items` and shows their current state. Constraint names follow the
naming convention in app/db/base.py.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "digests",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # The local day this digest is for, in SINAN_TIMEZONE
        sa.Column("digest_date", sa.Date(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        # The previous digest's generated_at; items created after it are new
        sa.Column("since", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_digests"),
        sa.UniqueConstraint("digest_date", name="uq_digests_digest_date"),
    )

    op.create_table(
        "digest_items",
        sa.Column("digest_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "section IN ('new', 'due_soon', 'needs_review')",
            name="ck_digest_items_section",
        ),
        sa.ForeignKeyConstraint(
            ["digest_id"],
            ["digests.id"],
            name="fk_digest_items_digest_id_digests",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name="fk_digest_items_item_id_items",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("digest_id", "section", "item_id", name="pk_digest_items"),
    )
    # Lets the item_id foreign key check run without a full scan
    op.create_index("ix_digest_items_item_id", "digest_items", ["item_id"])


def downgrade() -> None:
    op.drop_table("digest_items")
    op.drop_table("digests")
