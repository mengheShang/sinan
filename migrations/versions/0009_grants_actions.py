"""Create `grants` and `actions`, and widen `proposals` and `credentials`.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-19

The M3 action layer (ADR 0021, Issue #97). Owner decisions of 2026-11-19:

1. `proposals` holds every kind of proposal. `item_id`, `base_version`,
   `message_id` and `extraction_run_id` become nullable, and a CHECK states
   which of them each kind needs:

       item_update                      item, base version, email and run
       calendar_create, calendar_update item and base version, no run
       mark_read, unsubscribe           the target email, no item, no run

2. A one-click approval (Add to calendar, dismissing a notice) is a
   proposal inserted already `approved`, with `decided_at` set. The 0004
   trigger guards updates only, so it needs no change.
3. A grant's scope never changes. Only `paused_at` and `revoked_at` move,
   and a revoke is final; changing the senders or kinds means revoking the
   grant and creating a new one. `created_at` is always the inserting
   transaction's time, so the 90-day limit cannot be stretched by
   backdating.
4. Actions are never deleted, and after insert only their progress moves:
   status, attempts, timing, result and error, and `undo_payload` once.
   Allowed status moves:

       pending            -> running, refused
       running            -> succeeded, failed, needs_confirmation, pending
       needs_confirmation -> succeeded, failed

   `refused` is a recheck that failed before any external call;
   `running -> pending` is a transient error to retry later.

   An undo is a new action whose `undoes_action_id` points at the original
   (unique, so an action is undone at most once). An undo carries no
   proposal or grant: the owner's undo click is its authority, and audit
   events record it. Every other action has exactly one of a proposal or a
   grant.

`credentials.purpose` also gains `calendar`, the calendar-only token
(ADR 0021, decision 2).

Downgrade refuses while any row needs the new schema: a grant, an action,
a proposal of a new kind, or a calendar credential.

Constraint names follow the naming convention in app/db/base.py.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HASH = "'^[0-9a-f]{64}$'"

OLD_KINDS = "kind IN ('item_update')"
NEW_KINDS = (
    "kind IN ('item_update', 'calendar_create', 'calendar_update', 'mark_read', 'unsubscribe')"
)
SHAPE = """
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
OLD_PURPOSES = "purpose IN ('read', 'write')"
NEW_PURPOSES = "purpose IN ('read', 'write', 'calendar')"
# Nullable in 0009, required before it
LOOSENED = ("item_id", "base_version", "message_id", "extraction_run_id")

GRANT_KINDS = "cardinality(kinds) >= 1 AND kinds <@ ARRAY['mark_read', 'label', 'archive']::text[]"
# Lowercase exact addresses, separated by single spaces once joined
SENDERS = (
    "cardinality(senders) >= 1"
    " AND array_position(senders, NULL) IS NULL"
    " AND array_to_string(senders, ' ') = lower(array_to_string(senders, ' '))"
    " AND array_to_string(senders, ' ') ~ '^[^@ ]+@[^@ ]+( [^@ ]+@[^@ ]+)*$'"
)
ACTION_KINDS = (
    "kind IN ('mark_read', 'mark_unread', 'label_add', 'label_remove', 'archive', "
    "'unarchive', 'calendar_create', 'calendar_update', 'calendar_delete', 'unsubscribe')"
)
ACTION_STATUSES = (
    "status IN ('pending', 'running', 'refused', 'succeeded', 'failed', 'needs_confirmation')"
)
SOURCE = """
(undoes_action_id IS NULL AND num_nonnulls(proposal_id, grant_id) = 1)
OR (undoes_action_id IS NOT NULL AND proposal_id IS NULL AND grant_id IS NULL)
"""

GRANT_GUARD_FN = """
CREATE FUNCTION sinan_grant_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    fixed_old jsonb;
    fixed_new jsonb;
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.created_at := now();
        NEW.updated_at := now();
        RETURN NEW;
    END IF;
    IF TG_OP <> 'UPDATE' THEN
        RAISE EXCEPTION '% on grants is not allowed: grants are kept', TG_OP
            USING ERRCODE = 'restrict_violation';
    END IF;
    fixed_old := to_jsonb(OLD) - 'paused_at' - 'revoked_at' - 'updated_at';
    fixed_new := to_jsonb(NEW) - 'paused_at' - 'revoked_at' - 'updated_at';
    IF fixed_new IS DISTINCT FROM fixed_old THEN
        RAISE EXCEPTION 'a grant''s scope cannot change: revoke it and create a new one'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.revoked_at IS NOT NULL THEN
        IF NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
            OR NEW.paused_at IS DISTINCT FROM OLD.paused_at THEN
            RAISE EXCEPTION 'a revoked grant cannot change'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
"""

ACTION_GUARD_FN = """
CREATE FUNCTION sinan_action_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    moving text[] := ARRAY['status', 'attempts', 'started_at', 'finished_at',
        'result', 'error', 'undo_payload', 'updated_at'];
    fixed_old jsonb;
    fixed_new jsonb;
BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RAISE EXCEPTION '% on actions is not allowed: actions are kept', TG_OP
            USING ERRCODE = 'restrict_violation';
    END IF;
    fixed_old := to_jsonb(OLD) - moving;
    fixed_new := to_jsonb(NEW) - moving;
    IF fixed_new IS DISTINCT FROM fixed_old THEN
        RAISE EXCEPTION 'only the progress of an action can change'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.undo_payload IS NOT NULL
        AND NEW.undo_payload IS DISTINCT FROM OLD.undo_payload THEN
        RAISE EXCEPTION 'undo_payload is set once'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'pending' AND NEW.status IN ('running', 'refused') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'running' AND NEW.status IN
        ('succeeded', 'failed', 'needs_confirmation', 'pending') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'needs_confirmation' AND NEW.status IN ('succeeded', 'failed') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'action status cannot move from % to %', OLD.status, NEW.status
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""


class NewRowsExist(RuntimeError):
    """Downgrading would drop or break rows that need 0009."""


def ts(name: str, nullable: bool = True) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def stamp(name: str) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def uuid_pk() -> sa.Column[object]:
    return sa.Column(
        "id",
        sa.Uuid(),
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def widen_proposals() -> None:
    for column in LOOSENED:
        op.alter_column("proposals", column, nullable=True)
    op.drop_constraint("ck_proposals_kind", "proposals", type_="check")
    op.create_check_constraint("ck_proposals_kind", "proposals", NEW_KINDS)
    op.create_check_constraint("ck_proposals_shape", "proposals", SHAPE)
    op.drop_constraint("ck_credentials_purpose", "credentials", type_="check")
    op.create_check_constraint("ck_credentials_purpose", "credentials", NEW_PURPOSES)


def create_grants() -> None:
    op.create_table(
        "grants",
        uuid_pk(),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("senders", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("kinds", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "cycle_cap",
            sa.Integer(),
            server_default=sa.text("20"),
            nullable=False,
        ),
        ts("expires_at", nullable=False),
        ts("paused_at"),
        ts("revoked_at"),
        stamp("created_at"),
        stamp("updated_at"),
        sa.CheckConstraint(SENDERS, name="ck_grants_senders"),
        sa.CheckConstraint(GRANT_KINDS, name="ck_grants_kinds"),
        sa.CheckConstraint("cycle_cap BETWEEN 1 AND 20", name="ck_grants_cycle_cap"),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + interval '90 days'",
            name="ck_grants_expiry",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_grants_account_id_accounts",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_grants"),
    )
    op.create_index("ix_grants_account_id", "grants", ["account_id"])
    op.execute(GRANT_GUARD_FN)
    op.execute(
        "CREATE TRIGGER grants_guard BEFORE INSERT OR UPDATE OR DELETE ON grants "
        "FOR EACH ROW EXECUTE FUNCTION sinan_grant_guard()"
    )
    op.execute(
        "CREATE TRIGGER grants_no_truncate BEFORE TRUNCATE ON grants "
        "FOR EACH STATEMENT EXECUTE FUNCTION sinan_reject_change()"
    )


def fk(column: str, table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column],
        [f"{table}.id"],
        name=f"fk_actions_{column}_{table}",
        ondelete="RESTRICT",
    )


def create_actions() -> None:
    op.create_table(
        "actions",
        uuid_pk(),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=True),
        sa.Column("grant_id", sa.Uuid(), nullable=True),
        sa.Column("undoes_action_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("item_id", sa.Uuid(), nullable=True),
        sa.Column("target", postgresql.JSONB(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("undo_payload", postgresql.JSONB(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        ts("started_at"),
        ts("finished_at"),
        stamp("created_at"),
        stamp("updated_at"),
        sa.CheckConstraint(ACTION_KINDS, name="ck_actions_kind"),
        sa.CheckConstraint(ACTION_STATUSES, name="ck_actions_status"),
        sa.CheckConstraint(SOURCE, name="ck_actions_source"),
        sa.CheckConstraint("undoes_action_id <> id", name="ck_actions_not_self"),
        sa.CheckConstraint(f"idempotency_key ~ {HASH}", name="ck_actions_idempotency_key"),
        sa.CheckConstraint("jsonb_typeof(target) = 'object'", name="ck_actions_target"),
        sa.CheckConstraint("attempts >= 0", name="ck_actions_attempts"),
        sa.CheckConstraint(
            "kind <> 'unsubscribe' OR undo_payload IS NULL",
            name="ck_actions_no_undo_unsubscribe",
        ),
        fk("account_id", "accounts"),
        fk("proposal_id", "proposals"),
        fk("grant_id", "grants"),
        fk("undoes_action_id", "actions"),
        fk("message_id", "messages"),
        fk("item_id", "items"),
        sa.PrimaryKeyConstraint("id", name="pk_actions"),
        sa.UniqueConstraint("idempotency_key", name="uq_actions_idempotency_key"),
        sa.UniqueConstraint("undoes_action_id", name="uq_actions_undoes_action_id"),
    )
    # The Executor polls for pending actions, oldest first
    op.create_index("ix_actions_status_created_at", "actions", ["status", "created_at"])
    op.create_index("ix_actions_proposal_id", "actions", ["proposal_id"])
    # Grant caps count a grant's recent actions
    op.create_index("ix_actions_grant_id_created_at", "actions", ["grant_id", "created_at"])
    op.execute(ACTION_GUARD_FN)
    op.execute(
        "CREATE TRIGGER actions_guard BEFORE UPDATE OR DELETE ON actions "
        "FOR EACH ROW EXECUTE FUNCTION sinan_action_guard()"
    )
    op.execute(
        "CREATE TRIGGER actions_no_truncate BEFORE TRUNCATE ON actions "
        "FOR EACH STATEMENT EXECUTE FUNCTION sinan_reject_change()"
    )


def upgrade() -> None:
    widen_proposals()
    create_grants()
    create_actions()


def count(query: str) -> int:
    found: int = op.get_bind().execute(sa.text(query)).scalar_one()
    return found


def refuse_new_rows() -> None:
    checks = {
        "grants": "SELECT count(*) FROM grants",
        "actions": "SELECT count(*) FROM actions",
        "proposals of a new kind": "SELECT count(*) FROM proposals WHERE kind <> 'item_update'",
        "calendar credentials": "SELECT count(*) FROM credentials WHERE purpose = 'calendar'",
    }
    for what, query in checks.items():
        found = count(query)
        if found:
            raise NewRowsExist(
                f"Cannot downgrade below 0009: {found} {what} exist. "
                "Restore a backup taken before 0009 instead."
            )


def downgrade() -> None:
    refuse_new_rows()
    # Dropping a table also drops its triggers
    op.drop_table("actions")
    op.drop_table("grants")
    op.execute("DROP FUNCTION sinan_action_guard()")
    op.execute("DROP FUNCTION sinan_grant_guard()")
    op.drop_constraint("ck_credentials_purpose", "credentials", type_="check")
    op.create_check_constraint("ck_credentials_purpose", "credentials", OLD_PURPOSES)
    op.drop_constraint("ck_proposals_shape", "proposals", type_="check")
    op.drop_constraint("ck_proposals_kind", "proposals", type_="check")
    op.create_check_constraint("ck_proposals_kind", "proposals", OLD_KINDS)
    for column in LOOSENED:
        op.alter_column("proposals", column, nullable=False)
