"""The Worker's mail sync job: every connected account, one at a time.

The Worker only ever decrypts the `read` credential. In live mode it trades
that refresh token for a short-lived access token and reads Gmail with it.
An account that needs re-authorization is skipped until the owner reconnects.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.config import Settings
from app.db.accounts import list_accounts, refresh_read_token
from app.db.sync import sync_account
from app.providers import factory
from app.providers.demo_data import DEMO_EMAIL
from app.providers.mail import MailReader

log = logging.getLogger("sinan.worker.sync")

SessionFactory = Callable[[], Session]
# Returns a reader for the account, or None to skip it this round
ReaderFor = Callable[[Session, uuid.UUID, str], MailReader | None]
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def demo_readers(settings: Settings) -> ReaderFor:
    """Demo mode: one in-memory mailbox, kept across runs, for the demo account."""
    readers: dict[uuid.UUID, MailReader] = {}

    def reader_for(session: Session, account_id: uuid.UUID, email: str) -> MailReader | None:
        if email != DEMO_EMAIL:
            return None
        if account_id not in readers:
            readers[account_id] = factory.mail_reader(settings)
        return readers[account_id]

    return reader_for


def live_readers(settings: Settings) -> ReaderFor:
    """Live mode: refresh the read token, then read Gmail with the access token."""
    google = factory.google_oauth(settings)
    cipher = factory.token_cipher(settings)

    def reader_for(session: Session, account_id: uuid.UUID, email: str) -> MailReader | None:
        token = refresh_read_token(session, cipher, google, account_id)
        if token is None:
            log.warning("Account needs re-authorization; skipped")
            return None
        return factory.mail_reader(settings, token)

    return reader_for


def build_readers(settings: Settings) -> ReaderFor:
    if settings.sinan_mode == "demo":
        return demo_readers(settings)
    return live_readers(settings)


def sync_all(sessions: SessionFactory, reader_for: ReaderFor, clock: Clock = utc_now) -> int:
    """Sync every account that does not need re-authorization. Returns how many synced."""
    with sessions() as session:
        accounts = [a for a in list_accounts(session) if not a.needs_reauth]
    done = 0
    for account in accounts:
        with sessions() as session:
            reader = reader_for(session, account.id, account.email)
            # Keep a needs_reauth flag that the token refresh may have set
            session.commit()
            if reader is None:
                continue
            try:
                result = sync_account(session, reader, account.id, clock())
            except Exception as exc:
                # Class name only: exception text could quote mail content
                log.warning("Sync failed: %s", type(exc).__name__)
                continue
        log.info("Sync %s: %d listed, %d new", result.mode, result.listed, result.stored)
        done += 1
    return done
