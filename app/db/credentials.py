"""Save and load encrypted OAuth tokens.

Plaintext tokens exist only in memory. They are returned as SecretStr so an
accidental print or log shows `**********` instead of the value. The caller
owns the transaction and commits.
"""

import uuid
from datetime import datetime

from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.crypto import TokenCipher
from app.db.models import Credential


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


def load_credential(
    session: Session,
    cipher: TokenCipher,
    account_id: uuid.UUID,
    purpose: str,
) -> SecretStr | None:
    """Return the decrypted token, or None if there is none.

    Raises DecryptError if the stored ciphertext does not belong to this row
    or was made with another key.
    """
    query = select(Credential.ciphertext).where(
        Credential.account_id == account_id,
        Credential.purpose == purpose,
    )
    blob = session.execute(query).scalar_one_or_none()
    if blob is None:
        return None
    plaintext = cipher.decrypt(blob, account_id, purpose)
    return SecretStr(plaintext)
