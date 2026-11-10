"""Connected accounts: create on first authorization, track re-authorization.

The caller owns the transaction and commits.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.crypto import DecryptError, TokenCipher
from app.db.credentials import load_credential, save_credential
from app.db.models import Account, Credential, SyncState
from app.providers.oauth import OAuthClient, ReauthRequiredError

PROVIDER = "gmail"
READ = "read"


@dataclass(frozen=True)
class AccountView:
    id: uuid.UUID
    email: str
    role: str
    authorized_at: datetime | None
    needs_reauth: bool
    last_error: str | None
    last_sync_at: datetime | None


def connect_account(
    session: Session,
    cipher: TokenCipher,
    email: str,
    role: str,
    token: SecretStr,
    scopes: str,
    now: datetime,
) -> uuid.UUID:
    """Create or update the account, store its read token, clear needs_reauth."""
    stmt = insert(Account).values(provider=PROVIDER, email=email, role=role)
    stmt = stmt.on_conflict_do_update(
        index_elements=["provider", "email"],
        set_={"role": stmt.excluded.role, "updated_at": func.now()},
    )
    returning = stmt.returning(Account.id)
    account_id: uuid.UUID = session.execute(returning).scalar_one()
    save_credential(session, cipher, account_id, READ, token, scopes, now)
    sync = insert(SyncState).values(account_id=account_id, needs_reauth=False)
    sync = sync.on_conflict_do_update(
        index_elements=["account_id"],
        set_={"needs_reauth": False, "last_error": None, "updated_at": func.now()},
    )
    session.execute(sync)
    return account_id


def mark_reauth(session: Session, account_id: uuid.UUID, error: str) -> None:
    """Flag the account until the owner authorizes again. `error` is a code only."""
    stmt = insert(SyncState).values(
        account_id=account_id,
        needs_reauth=True,
        last_error=error,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["account_id"],
        set_={"needs_reauth": True, "last_error": error, "updated_at": func.now()},
    )
    session.execute(stmt)


def list_accounts(session: Session) -> list[AccountView]:
    query = (
        select(
            Account.id,
            Account.email,
            Account.role,
            Credential.authorized_at,
            SyncState.needs_reauth,
            SyncState.last_error,
            SyncState.last_success_at,
        )
        .outerjoin(
            Credential,
            (Credential.account_id == Account.id) & (Credential.purpose == READ),
        )
        .outerjoin(SyncState, SyncState.account_id == Account.id)
        .order_by(Account.created_at)
    )
    rows = session.execute(query).all()
    views = []
    for row in rows:
        reauth = bool(row.needs_reauth) or row.authorized_at is None
        view = AccountView(
            row.id,
            row.email,
            row.role,
            row.authorized_at,
            reauth,
            row.last_error,
            row.last_success_at,
        )
        views.append(view)
    return views


def refresh_read_token(
    session: Session,
    cipher: TokenCipher,
    client: OAuthClient,
    account_id: uuid.UUID,
) -> SecretStr | None:
    """Return a fresh access token, or None after flagging the account.

    An expired or revoked refresh token (`invalid_grant`), a missing one, or
    one that no longer decrypts all mean the owner has to authorize again.
    Other errors (network, server) propagate so a later retry can succeed.
    """
    try:
        token = load_credential(session, cipher, account_id, READ)
    except DecryptError:
        mark_reauth(session, account_id, "decrypt_failed")
        return None
    if token is None:
        mark_reauth(session, account_id, "no_credential")
        return None
    try:
        return client.refresh(token)
    except ReauthRequiredError:
        mark_reauth(session, account_id, "invalid_grant")
        return None
