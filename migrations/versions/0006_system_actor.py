"""Allow the `system` actor on item versions.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-13

A version that no person and no extraction made, such as an item moving to
needs_review because its proposed change expired unanswered (ADR 0014), is
recorded with actor `system`. Only the CHECK on `item_versions.actor`
changes: `extraction` and `user` keep their meaning, and the rule that an
extraction version points at its email and evidence is unchanged.

Downgrade refuses while any `system` version exists. `item_versions` is
append-only (a trigger rejects UPDATE and DELETE), so those rows cannot be
rewritten or removed, and the old CHECK could not be restored over them.

Constraint names follow the naming convention in app/db/base.py.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = "ck_item_versions_actor"
TABLE = "item_versions"


class SystemVersionsExist(RuntimeError):
    """Downgrading would drop the meaning of rows that cannot be removed."""


def upgrade() -> None:
    op.drop_constraint(NAME, TABLE, type_="check")
    op.create_check_constraint(NAME, TABLE, "actor IN ('extraction', 'user', 'system')")


def downgrade() -> None:
    query = sa.text("SELECT count(*) FROM item_versions WHERE actor = 'system'")
    count = op.get_bind().execute(query).scalar_one()
    if count:
        raise SystemVersionsExist(
            f"Cannot downgrade below 0006: {count} item_versions rows have actor "
            "'system', and item_versions is append-only. Restore a backup taken "
            "before 0006 instead."
        )
    op.drop_constraint(NAME, TABLE, type_="check")
    op.create_check_constraint(NAME, TABLE, "actor IN ('extraction', 'user')")
