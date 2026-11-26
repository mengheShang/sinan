"""The live home page, computed when it is read (ADR 0022, decisions 1 and 2).

Nothing is stored for it. Each active deadline or to-do is in exactly one
list, or in none when ADR 0015 puts it in the Past view:

- due: items with a due time, and every item that needs review.
  - Overdue items first (ADR 0015's window), soonest due first.
  - Then open items, soonest due first.
  - Then items that need review, soonest due first, those with no due time
    last. An overdue item that needs review stays with the overdue ones.
- todo: open items with no due time, newest first.
- notices: every open notice, most recently received first (ADR 0017); the
  Dashboard groups them by category (ADR 0020).

Items first found today, by the local day in SINAN_TIMEZONE, are marked
`added_today`, in whichever list they are in. It depends only on
`created_at`, so it needs no stored state.

The page also counts the proposals that expired unanswered in the last
7 days. Reading writes nothing.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db.digest import (
    ACTIVE,
    ItemView,
    expired_between,
    list_items,
    open_notices,
    pending_items,
    phase_of,
)
from app.items.timing import SHOWN

EXPIRED_WINDOW = timedelta(days=7)
# Ranks in the due list
OVERDUE, OPEN, REVIEW = 0, 1, 2


@dataclass(frozen=True)
class Entry:
    view: ItemView
    overdue: bool
    added_today: bool


@dataclass(frozen=True)
class HomeView:
    due: list[Entry]
    todo: list[Entry]
    notices: list[Entry]
    # Proposals that expired unanswered in the last 7 days
    expired_proposals: int


def is_added_today(created_at: datetime, now: datetime, zone: ZoneInfo) -> bool:
    """Whether the item was first found on today's local date."""
    made = created_at.astimezone(zone).date()
    today = now.astimezone(zone).date()
    return made == today


def rank(entry: Entry) -> int:
    if entry.overdue:
        return OVERDUE
    if entry.view.status == "needs_review":
        return REVIEW
    return OPEN


def due_key(entry: Entry) -> tuple[int, bool, datetime, str]:
    view = entry.view
    when = view.due_at or view.created_at
    return (rank(entry), view.due_at is None, when, view.title)


def todo_key(entry: Entry) -> tuple[datetime, str]:
    return (entry.view.created_at, entry.view.title)


def belongs_in_due(view: ItemView) -> bool:
    return view.due_at is not None or view.status == "needs_review"


def read_home(session: Session, now: datetime, zone: ZoneInfo) -> HomeView:
    pending = pending_items(session, now)
    due: list[Entry] = []
    todo: list[Entry] = []
    for view in list_items(session, ACTIVE):
        where = phase_of(view, now, pending)
        if where not in SHOWN:
            continue
        added = is_added_today(view.created_at, now, zone)
        entry = Entry(view, where == "overdue", added)
        target = due if belongs_in_due(view) else todo
        target.append(entry)
    due.sort(key=due_key)
    todo.sort(key=todo_key, reverse=True)
    notices = [
        Entry(view, False, is_added_today(view.created_at, now, zone))
        for view in open_notices(session)
    ]
    expired = expired_between(session, now - EXPIRED_WINDOW, now)
    return HomeView(due, todo, notices, expired)
