"""Credential store tests against a throwaway database (see conftest.py)."""

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.crypto import DecryptError, TokenCipher, generate_key
from app.db.credentials import load_credential, save_credential
from app.db.models import Account, Credential

pytestmark = pytest.mark.db

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
TOKEN = SecretStr("fake-refresh-token-for-tests")


@pytest.fixture
def session(conn: Connection) -> Session:
    return Session(bind=conn, join_transaction_mode="create_savepoint")


@pytest.fixture
def cipher() -> TokenCipher:
    return TokenCipher(generate_key())


@pytest.fixture
def account(session: Session) -> uuid.UUID:
    stmt = sa.insert(Account).values(
        provider="gmail",
        email="student@example.edu",
        role="school",
    )
    stmt = stmt.returning(Account.id)
    key: uuid.UUID = session.execute(stmt).scalar_one()
    return key


def save(session: Session, cipher: TokenCipher, account: uuid.UUID, token: SecretStr) -> None:
    save_credential(session, cipher, account, "read", token, "gmail.readonly", NOW)


def test_save_then_load(session: Session, cipher: TokenCipher, account: uuid.UUID) -> None:
    save(session, cipher, account, TOKEN)
    loaded = load_credential(session, cipher, account, "read")
    assert loaded is not None
    assert loaded.get_secret_value() == TOKEN.get_secret_value()


def test_database_holds_only_ciphertext(
    session: Session,
    cipher: TokenCipher,
    account: uuid.UUID,
) -> None:
    save(session, cipher, account, TOKEN)
    blob = session.execute(sa.select(Credential.ciphertext)).scalar_one()
    assert TOKEN.get_secret_value().encode() not in blob


def test_save_replaces_existing(session: Session, cipher: TokenCipher, account: uuid.UUID) -> None:
    save(session, cipher, account, TOKEN)
    newer = SecretStr("fake-refresh-token-newer")
    save(session, cipher, account, newer)
    count = session.execute(sa.select(sa.func.count()).select_from(Credential)).scalar_one()
    loaded = load_credential(session, cipher, account, "read")
    assert count == 1
    assert loaded is not None
    assert loaded.get_secret_value() == newer.get_secret_value()


def test_missing_credential_is_none(
    session: Session,
    cipher: TokenCipher,
    account: uuid.UUID,
) -> None:
    assert load_credential(session, cipher, account, "write") is None


def test_ciphertext_copied_to_write_row_fails(
    session: Session,
    cipher: TokenCipher,
    account: uuid.UUID,
) -> None:
    save(session, cipher, account, TOKEN)
    blob = session.execute(sa.select(Credential.ciphertext)).scalar_one()
    copy = sa.insert(Credential).values(
        account_id=account,
        purpose="write",
        ciphertext=blob,
        scopes="gmail.modify",
        authorized_at=NOW,
    )
    session.execute(copy)
    with pytest.raises(DecryptError):
        load_credential(session, cipher, account, "write")


def test_loaded_token_is_masked_in_repr(
    session: Session,
    cipher: TokenCipher,
    account: uuid.UUID,
) -> None:
    save(session, cipher, account, TOKEN)
    loaded = load_credential(session, cipher, account, "read")
    assert TOKEN.get_secret_value() not in repr(loaded)
    assert TOKEN.get_secret_value() not in str(loaded)
