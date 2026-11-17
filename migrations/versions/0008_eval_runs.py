"""Create the table for the official test-set run's raw replies.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-17

Issue #87: the first official test-set run keeps its raw model replies in
the local database, never in the repository, so `--set test --recorded`
reproduces its numbers exactly without calling the model. One row is one
run: the replies keyed by sample id, the link step's replies keyed by found
item uid, and the `Authentication-Results` header read again from Gmail for
each sample, together with what the run depended on (set version, label
time, model, prompt versions and hashes, a hash of the rules, the commit).

A stored run can be removed but never altered: the shared
`sinan_reject_change()` trigger from 0002 rejects UPDATE and TRUNCATE.
DELETE is allowed on purpose (owner decision, 2026-11-17), because the rows
hold text derived from real mail and the owner must be able to purge them.
The newest row of a set version is the official run; a second official run
is a new row, which the runner refuses unless asked on purpose. Once every
row of a set version is deleted, a new official run needs no flag.

Constraint names follow the naming convention in app/db/base.py.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HASH = "'^[0-9a-f]{64}$'"


def upgrade() -> None:
    op.create_table(
        "eval_runs",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("eval_set", sa.Text(), nullable=False),
        sa.Column("set_version", sa.Integer(), nullable=False),
        sa.Column("labels_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("prompt_hash", sa.Text(), nullable=False),
        sa.Column("link_prompt_version", sa.Text(), nullable=False),
        sa.Column("link_prompt_hash", sa.Text(), nullable=False),
        sa.Column("rules_hash", sa.Text(), nullable=False),
        sa.Column("commit", sa.Text(), nullable=False),
        sa.Column("replies", postgresql.JSONB(), nullable=False),
        sa.Column("link_replies", postgresql.JSONB(), nullable=False),
        sa.Column("auth", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("eval_set IN ('dev', 'test')", name="ck_eval_runs_eval_set"),
        sa.CheckConstraint("set_version >= 1", name="ck_eval_runs_set_version"),
        sa.CheckConstraint(f"prompt_hash ~ {HASH}", name="ck_eval_runs_prompt_hash"),
        sa.CheckConstraint(f"link_prompt_hash ~ {HASH}", name="ck_eval_runs_link_prompt_hash"),
        sa.CheckConstraint(f"rules_hash ~ {HASH}", name="ck_eval_runs_rules_hash"),
        sa.CheckConstraint("jsonb_typeof(replies) = 'object'", name="ck_eval_runs_replies"),
        sa.CheckConstraint(
            "jsonb_typeof(link_replies) = 'object'", name="ck_eval_runs_link_replies"
        ),
        sa.CheckConstraint("jsonb_typeof(auth) = 'object'", name="ck_eval_runs_auth"),
        sa.PrimaryKeyConstraint("id", name="pk_eval_runs"),
    )
    op.create_index(
        "ix_eval_runs_eval_set_set_version",
        "eval_runs",
        ["eval_set", "set_version"],
    )
    op.execute(
        # UPDATE only: DELETE stays allowed so the owner can purge stored runs
        "CREATE TRIGGER eval_runs_no_update BEFORE UPDATE ON eval_runs "
        "FOR EACH ROW EXECUTE FUNCTION sinan_reject_change()"
    )
    op.execute(
        "CREATE TRIGGER eval_runs_no_truncate BEFORE TRUNCATE ON eval_runs "
        "FOR EACH STATEMENT EXECUTE FUNCTION sinan_reject_change()"
    )


def downgrade() -> None:
    # Dropping the table also drops its triggers and the stored replies
    op.drop_table("eval_runs")
