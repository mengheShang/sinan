"""The Worker's digest job: today's digest, once, after the morning hour.

It runs at the end of every cycle, after sync and extraction, so a digest
includes the newest items. It is shown only in the Dashboard, never sent.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.actions.proposals import sweep
from app.config import Settings
from app.db.digest import make_digest
from app.db.models import Item
from apps.worker.sync_job import SessionFactory

log = logging.getLogger("sinan.worker.digest")

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def has_items(session: Session) -> bool:
    return session.execute(select(Item.id).limit(1)).first() is not None


def digest_today(sessions: SessionFactory, settings: Settings, clock: Clock = utc_now) -> bool:
    """Make today's digest if it is due and missing. Returns whether one was made."""
    force = settings.sinan_mode == "demo"
    with sessions() as session:
        # The demo makes its digest at once, but only after the demo mailbox is
        # connected and extracted; an empty one would stay empty all day
        if force and not has_items(session):
            return False
        try:
            now = clock()
            # Expire and supersede first, so the digest counts and flags them
            sweep(session, now, "worker")
            session.commit()
            made = make_digest(session, now, settings.zone(), settings.sinan_digest_hour, force)
        except Exception as exc:
            log.warning("Digest failed: %s", type(exc).__name__)
            return False
    if made is not None:
        log.info("Digest made for %s", made.digest_date.isoformat())
    return made is not None
