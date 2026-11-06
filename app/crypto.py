"""Encryption for OAuth tokens at rest.

Tokens are encrypted with AES-256-GCM. Each ciphertext is bound to its
account and purpose through GCM associated data, so a ciphertext copied into
another row (for example a read token moved into the write slot) fails to
decrypt instead of granting more access.

Stored format: 1 version byte, 12-byte random nonce, then ciphertext and tag.
"""

import base64
import binascii
import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = b"\x01"
NONCE_SIZE = 12
KEY_SIZE = 32


class InvalidKeyError(ValueError):
    """The encryption key is missing or malformed."""


class DecryptError(Exception):
    """The ciphertext is corrupt, was made with another key, or belongs to another row."""


def generate_key() -> str:
    """Return a new random key, encoded for SINAN_ENCRYPTION_KEY."""
    raw = os.urandom(KEY_SIZE)
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_key(encoded: str) -> bytes:
    """Decode a key from SINAN_ENCRYPTION_KEY. Raises InvalidKeyError if invalid."""
    try:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise InvalidKeyError("SINAN_ENCRYPTION_KEY is not valid base64") from exc
    if len(raw) != KEY_SIZE:
        raise InvalidKeyError("SINAN_ENCRYPTION_KEY must decode to 32 bytes")
    return raw


def bind(account_id: uuid.UUID, purpose: str) -> bytes:
    """Associated data that ties a ciphertext to one credentials row."""
    label = f"sinan-credential:{account_id}:{purpose}"
    return label.encode("ascii")


class TokenCipher:
    def __init__(self, encoded_key: str) -> None:
        key = decode_key(encoded_key)
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str, account_id: uuid.UUID, purpose: str) -> bytes:
        nonce = os.urandom(NONCE_SIZE)
        data = plaintext.encode("utf-8")
        aad = bind(account_id, purpose)
        sealed = self._aead.encrypt(nonce, data, aad)
        return VERSION + nonce + sealed

    def decrypt(self, blob: bytes, account_id: uuid.UUID, purpose: str) -> str:
        if blob[:1] != VERSION:
            raise DecryptError("unknown ciphertext version")
        nonce = blob[1 : 1 + NONCE_SIZE]
        sealed = blob[1 + NONCE_SIZE :]
        aad = bind(account_id, purpose)
        try:
            data = self._aead.decrypt(nonce, sealed, aad)
        except InvalidTag as exc:
            raise DecryptError("ciphertext failed authentication") from exc
        return data.decode("utf-8")


if __name__ == "__main__":
    # Prints a fresh key. Run it yourself and put the output in your own .env.
    print(generate_key())
