import base64
import uuid

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.crypto import DecryptError, InvalidKeyError, TokenCipher, decode_key, generate_key

ACCOUNT = uuid.UUID("00000000-0000-4000-8000-000000000001")
OTHER = uuid.UUID("00000000-0000-4000-8000-000000000002")
TOKEN = "fake-refresh-token-for-tests"


@pytest.fixture
def cipher() -> TokenCipher:
    return TokenCipher(generate_key())


def test_round_trip(cipher: TokenCipher) -> None:
    blob = cipher.encrypt(TOKEN, ACCOUNT, "read")
    assert cipher.decrypt(blob, ACCOUNT, "read") == TOKEN


def test_ciphertext_hides_plaintext(cipher: TokenCipher) -> None:
    blob = cipher.encrypt(TOKEN, ACCOUNT, "read")
    assert TOKEN.encode() not in blob


def test_same_token_encrypts_differently(cipher: TokenCipher) -> None:
    first = cipher.encrypt(TOKEN, ACCOUNT, "read")
    second = cipher.encrypt(TOKEN, ACCOUNT, "read")
    assert first != second


def test_wrong_key_fails(cipher: TokenCipher) -> None:
    blob = cipher.encrypt(TOKEN, ACCOUNT, "read")
    other = TokenCipher(generate_key())
    with pytest.raises(DecryptError):
        other.decrypt(blob, ACCOUNT, "read")


def test_tampered_ciphertext_fails(cipher: TokenCipher) -> None:
    blob = bytearray(cipher.encrypt(TOKEN, ACCOUNT, "read"))
    blob[-1] ^= 0x01
    with pytest.raises(DecryptError):
        cipher.decrypt(bytes(blob), ACCOUNT, "read")


def test_read_token_cannot_be_used_as_write_token(cipher: TokenCipher) -> None:
    blob = cipher.encrypt(TOKEN, ACCOUNT, "read")
    with pytest.raises(DecryptError):
        cipher.decrypt(blob, ACCOUNT, "write")


def test_token_cannot_move_to_another_account(cipher: TokenCipher) -> None:
    blob = cipher.encrypt(TOKEN, ACCOUNT, "read")
    with pytest.raises(DecryptError):
        cipher.decrypt(blob, OTHER, "read")


def test_unknown_version_fails(cipher: TokenCipher) -> None:
    blob = cipher.encrypt(TOKEN, ACCOUNT, "read")
    with pytest.raises(DecryptError):
        cipher.decrypt(b"\x02" + blob[1:], ACCOUNT, "read")


@pytest.mark.parametrize(
    "bad",
    ["", "not base64!", base64.urlsafe_b64encode(b"short").decode()],
)
def test_bad_keys_are_rejected(bad: str) -> None:
    with pytest.raises(InvalidKeyError):
        decode_key(bad)


def test_demo_mode_needs_no_key() -> None:
    settings = Settings(sinan_mode="demo")
    assert settings.sinan_encryption_key is None


def test_live_mode_requires_key() -> None:
    with pytest.raises(ValidationError, match="SINAN_ENCRYPTION_KEY is required"):
        Settings(sinan_mode="live")


def test_live_mode_rejects_bad_key() -> None:
    with pytest.raises(ValidationError, match="32 bytes"):
        Settings(sinan_mode="live", sinan_encryption_key=SecretStr(""))


def test_live_mode_accepts_valid_key() -> None:
    key = SecretStr(generate_key())
    settings = Settings(sinan_mode="live", sinan_encryption_key=key)
    assert settings.sinan_mode == "live"


def test_key_is_hidden_in_repr() -> None:
    key = generate_key()
    settings = Settings(sinan_mode="live", sinan_encryption_key=SecretStr(key))
    assert key not in repr(settings)
