"""Allow the `notice` kind on items.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-13

A notice is optional or general mail, such as an event invitation or a
safety alert, that the owner does not have to act on (ADR 0017). Only the
CHECK on `items.kind` changes: `deadline` and `todo` keep their meaning, and
no column, table or other constraint is touched. Which digest shows a
notice is computed on read, so `digest_items.section` is unchanged.

Downgrade refuses while any item is a notice. Rewriting those rows would
put them back into to-do lists without anyone deciding so, and their first
`item_versions` row, which is append-only, would still say `notice`.

Constraint names follow the naming convention in app/db/base.py.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = "ck_items_kind"
TABLE = "items"


class NoticesExist(RuntimeError):
    """Downgrading would leave rows the old CHECK cannot hold."""


def upgrade() -> None:
    op.drop_constraint(NAME, TABLE, type_="check")
    op.create_check_constraint(NAME, TABLE, "kind IN ('deadline', 'todo', 'notice')")


def downgrade() -> None:
    query = sa.text("SELECT count(*) FROM items WHERE kind = 'notice'")
    count = op.get_bind().execute(query).scalar_one()
    if count:
        raise NoticesExist(
            f"Cannot downgrade below 0007: {count} items have kind 'notice'. "
            "Make them to-dos from the Dashboard first, or restore a backup "
            "taken before 0007."
        )
    op.drop_constraint(NAME, TABLE, type_="check")
    op.create_check_constraint(NAME, TABLE, "kind IN ('deadline', 'todo')")
