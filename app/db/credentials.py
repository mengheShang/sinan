"""Save and load encrypted OAuth tokens.

Plaintext tokens exist only in memory. They are returned as SecretStr so an
accidental print or log shows `**********` instead of the value. The caller
owns the transaction and commits.

Read tokens are encrypted with SINAN_ENCRYPTION_KEY (app/crypto.py). Write
and calendar tokens are sealed with the Executor's public key
(app/sealing.py, ADR 0021): `save_sealed` is the only way they are stored,
and nothing here opens them; only the Executor can.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.crypto import TokenCipher
from app.db.models import Credential
from app.sealing import Sealer

# Purposes whose token is sealed for the Executor (ADR 0021)
SEALED = ("write", "calendar")


def save_credential(
    session: Session,
    cipher: TokenCipher,
    account_id: uuid.UUID,
    purpose: str,
    token: SecretStr,
    scopes: str,
    authorized_at: datetime,
) -> None:
    """Insert or replace the token for one account and purpose."""
    plaintext = token.get_secret_value()
    blob = cipher.encrypt(plaintext, account_id, purpose)
    values = {
        "account_id": account_id,
        "purpose": purpose,
        "ciphertext": blob,
        "scopes": scopes,
        "authorized_at": authorized_at,
    }
    stmt = insert(Credential).values(**values)
    changes = {
        "ciphertext": stmt.excluded.ciphertext,
        "scopes": stmt.excluded.scopes,
        "authorized_at": stmt.excluded.authorized_at,
        "updated_at": func.now(),
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=["account_id", "purpose"],
        set_=changes,
    )
    session.execute(stmt)


def save_sealed(
    session: Session,
    sealer: Sealer,
    account_id: uuid.UUID,
    purpose: str,
    token: SecretStr,
    scopes: str,
    authorized_at: datetime,
) -> None:
    """Insert or replace a write or calendar token, sealed for the Executor.

    A new authorization clears the token's health flags (migration 0011).
    """
    if purpose not in SEALED:
        raise ValueError(f"only {SEALED} tokens are sealed")
    plaintext = token.get_secret_value()
    blob = sealer.seal(plaintext, account_id, purpose)
    values = {
        "account_id": account_id,
        "purpose": purpose,
        "ciphertext": blob,
        "scopes": scopes,
        "authorized_at": authorized_at,
        "needs_reauth": False,
        "last_error": None,
    }
    stmt = insert(Credential).values(**values)
    changes = {
        "ciphertext": stmt.excluded.ciphertext,
        "scopes": stmt.excluded.scopes,
        "authorized_at": stmt.excluded.authorized_at,
        "needs_reauth": False,
        "last_error": None,
        "updated_at": func.now(),
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=["account_id", "purpose"],
        set_=changes,
    )
    session.execute(stmt)


@dataclass(frozen=True)
class Health:
    """One sealed token's age and flags; never the token."""

    authorized_at: datetime
    needs_reauth: bool
    last_error: str | None


def sealed_health(session: Session) -> dict[tuple[uuid.UUID, str], Health]:
    """Every write and calendar token's health, by account and purpose."""
    query = select(
        Credential.account_id,
        Credential.purpose,
        Credential.authorized_at,
        Credential.needs_reauth,
        Credential.last_error,
    ).where(Credential.purpose.in_(SEALED))
    found = {}
    for row in session.execute(query).all():
        found[(row.account_id, row.purpose)] = Health(
            row.authorized_at, row.needs_reauth, row.last_error
        )
    return found


def load_credential(
    session: Session,
    cipher: TokenCipher,
    account_id: uuid.UUID,
    purpose: str,
) -> SecretStr | None:
    """Return the decrypted read token, or None if there is none.

    Raises DecryptError if the stored ciphertext does not belong to this row
    or was made with another key. Refuses a write or calendar purpose: those
    are sealed, and only the Executor opens them.
    """
    if purpose in SEALED:
        raise ValueError("sealed tokens are opened by the Executor only")
    query = select(Credential.ciphertext).where(
        Credential.account_id == account_id,
        Credential.purpose == purpose,
    )
    blob = session.execute(query).scalar_one_or_none()
    if blob is None:
        return None
    plaintext = cipher.decrypt(blob, account_id, purpose)
    return SecretStr(plaintext)
