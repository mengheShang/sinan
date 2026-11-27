"""Add `needs_reauth` and `last_error` to `credentials`.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-27

ADR 0021, decision 2, and Issue #98 (owner decision of 2026-11-19: each
token is flagged on its own). Write and calendar tokens are used only by
the Executor, which cannot touch `sync_states` health for them, so each
credential row carries its own:

- `needs_reauth`: the provider refused the token (`invalid_grant`, or it
  no longer opens); the owner must authorize again. Default false.
- `last_error`: a short error code, never a token or message content.

Both are cleared when the owner authorizes again. Read tokens keep using
`sync_states`, unchanged.

Downgrade drops both columns; the flags they held are lost, and a token
the provider has refused shows as healthy until its next use.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "credentials",
        sa.Column("needs_reauth", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("credentials", sa.Column("last_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("credentials", "last_error")
    op.drop_column("credentials", "needs_reauth")
