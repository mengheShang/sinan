"""The sealed box for write and calendar tokens (ADR 0021, decision 1).

Every key and token here is generated or fake.
"""

import uuid

import pytest

from app import sealing
from app.crypto import DecryptError, InvalidKeyError, TokenCipher, generate_key
from app.sealing import HEADER_SIZE, KEY_SIZE, Opener, Sealer, generate_pair

ACCOUNT = uuid.UUID("00000000-0000-4000-8000-000000000001")
OTHER = uuid.UUID("00000000-0000-4000-8000-000000000002")
TOKEN = "fake-write-refresh-token-for-tests"


@pytest.fixture
def pair() -> tuple[Sealer, Opener]:
    private, public = generate_pair()
    return Sealer(public), Opener(private)


def test_round_trip(pair: tuple[Sealer, Opener]) -> None:
    sealer, opener = pair
    blob = sealer.seal(TOKEN, ACCOUNT, "write")
    assert opener.open(blob, ACCOUNT, "write") == TOKEN


def test_blob_hides_the_token(pair: tuple[Sealer, Opener]) -> None:
    sealer, _ = pair
    blob = sealer.seal(TOKEN, ACCOUNT, "write")
    assert TOKEN.encode() not in blob


def test_each_seal_uses_a_fresh_ephemeral_key(pair: tuple[Sealer, Opener]) -> None:
    sealer, _ = pair
    first = sealer.seal(TOKEN, ACCOUNT, "write")
    second = sealer.seal(TOKEN, ACCOUNT, "write")
    assert first[1 : 1 + KEY_SIZE] != second[1 : 1 + KEY_SIZE]
    assert first != second


def test_only_the_matching_private_key_opens(pair: tuple[Sealer, Opener]) -> None:
    sealer, _ = pair
    blob = sealer.seal(TOKEN, ACCOUNT, "write")
    stranger, _ = generate_pair()
    with pytest.raises(DecryptError):
        Opener(stranger).open(blob, ACCOUNT, "write")


def test_the_sealer_cannot_open() -> None:
    """The API's side has the public key only, and no way to open a blob."""
    assert not hasattr(Sealer, "open")
    assert not hasattr(Sealer, "decrypt")


def test_the_read_token_key_cannot_open(pair: tuple[Sealer, Opener]) -> None:
    sealer, _ = pair
    blob = sealer.seal(TOKEN, ACCOUNT, "write")
    with pytest.raises(DecryptError):
        TokenCipher(generate_key()).decrypt(blob, ACCOUNT, "write")


def test_a_read_token_blob_does_not_open(pair: tuple[Sealer, Opener]) -> None:
    _, opener = pair
    blob = TokenCipher(generate_key()).encrypt(TOKEN, ACCOUNT, "write")
    with pytest.raises(DecryptError):
        opener.open(blob, ACCOUNT, "write")


@pytest.mark.parametrize(("account", "purpose"), [(OTHER, "write"), (ACCOUNT, "calendar")])
def test_bound_to_its_row(pair: tuple[Sealer, Opener], account: uuid.UUID, purpose: str) -> None:
    sealer, opener = pair
    blob = sealer.seal(TOKEN, ACCOUNT, "write")
    with pytest.raises(DecryptError):
        opener.open(blob, account, purpose)


@pytest.mark.parametrize("index", [0, 1, HEADER_SIZE - 1, HEADER_SIZE, -1])
def test_any_changed_byte_fails(pair: tuple[Sealer, Opener], index: int) -> None:
    sealer, opener = pair
    blob = bytearray(sealer.seal(TOKEN, ACCOUNT, "write"))
    blob[index] ^= 0x01
    with pytest.raises(DecryptError):
        opener.open(bytes(blob), ACCOUNT, "write")


def test_a_truncated_blob_fails(pair: tuple[Sealer, Opener]) -> None:
    sealer, opener = pair
    blob = sealer.seal(TOKEN, ACCOUNT, "write")
    with pytest.raises(DecryptError):
        opener.open(blob[:HEADER_SIZE], ACCOUNT, "write")


def test_a_low_order_ephemeral_key_fails(pair: tuple[Sealer, Opener]) -> None:
    sealer, opener = pair
    blob = sealer.seal(TOKEN, ACCOUNT, "write")
    forged = blob[:1] + bytes(KEY_SIZE) + blob[1 + KEY_SIZE :]
    with pytest.raises(DecryptError):
        opener.open(forged, ACCOUNT, "write")


def test_opener_reports_its_public_key() -> None:
    private, public = generate_pair()
    assert Opener(private).public_key() == public


@pytest.mark.parametrize("bad", ["", "not base64 !", sealing.encode(b"short")])
def test_malformed_keys_are_refused(bad: str) -> None:
    with pytest.raises(InvalidKeyError):
        Sealer(bad)
    with pytest.raises(InvalidKeyError):
        Opener(bad)


def test_key_errors_never_echo_the_key() -> None:
    bad = sealing.encode(b"secret-looking-value")
    with pytest.raises(InvalidKeyError) as caught:
        Opener(bad)
    assert bad not in str(caught.value)
