"""Seal write tokens so that only the Executor can open them (ADR 0021, decision 1).

The API holds only the Executor's public key. In the OAuth callback it seals
the refresh token and stores it; it has no way to open it again. Only the
Executor holds the private key.

Sealing, for each token:

1. Make a fresh ephemeral X25519 key pair.
2. Agree a shared secret between the ephemeral private key and the
   Executor's public key.
3. Derive a 32-byte AES key from it with HKDF-SHA256. The salt is the
   ephemeral public key followed by the Executor's public key, so the key
   belongs to this one exchange and this one recipient.
4. Encrypt with AES-256-GCM. As in app/crypto.py, the associated data binds
   the ciphertext to its account and purpose, so a sealed token copied into
   another row fails to open.

Opening repeats steps 2 and 3 from the Executor's side, with its private key
and the ephemeral public key stored in the blob.

Stored format: 1 version byte, the 32-byte ephemeral public key, a 12-byte
random nonce, then ciphertext and tag. The version byte differs from
app/crypto.py's, so neither module accepts the other's format.
"""

import base64
import binascii
import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.crypto import DecryptError, InvalidKeyError, bind

VERSION = b"\x02"
KEY_SIZE = 32
NONCE_SIZE = 12
HEADER_SIZE = 1 + KEY_SIZE + NONCE_SIZE
INFO = b"sinan-sealed-token-v1"


def encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode(encoded: str, name: str) -> bytes:
    """Decode a 32-byte key from the named variable. Raises InvalidKeyError."""
    try:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise InvalidKeyError(f"{name} is not valid base64") from exc
    if len(raw) != KEY_SIZE:
        raise InvalidKeyError(f"{name} must decode to 32 bytes")
    return raw


def public_bytes(key: X25519PublicKey) -> bytes:
    return key.public_bytes_raw()


def generate_pair() -> tuple[str, str]:
    """A new key pair, encoded: (private, public)."""
    private = X25519PrivateKey.generate()
    public = private.public_key()
    return encode(private.private_bytes_raw()), encode(public_bytes(public))


def derive(shared: bytes, ephemeral: bytes, recipient: bytes) -> AESGCM:
    """The AES-GCM key for one exchange, from the X25519 shared secret."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=ephemeral + recipient,
        info=INFO,
    )
    key = hkdf.derive(shared)
    return AESGCM(key)


class Sealer:
    """Seals with the Executor's public key. It cannot open anything."""

    def __init__(self, encoded_public: str) -> None:
        raw = decode(encoded_public, "SINAN_EXECUTOR_PUBLIC_KEY")
        self._recipient = X25519PublicKey.from_public_bytes(raw)

    def seal(self, plaintext: str, account_id: uuid.UUID, purpose: str) -> bytes:
        ephemeral = X25519PrivateKey.generate()
        shared = ephemeral.exchange(self._recipient)
        eph_raw = public_bytes(ephemeral.public_key())
        aead = derive(shared, eph_raw, public_bytes(self._recipient))
        nonce = os.urandom(NONCE_SIZE)
        data = plaintext.encode("utf-8")
        aad = bind(account_id, purpose)
        sealed = aead.encrypt(nonce, data, aad)
        return VERSION + eph_raw + nonce + sealed


class Opener:
    """Opens sealed tokens with the Executor's private key. Executor only."""

    def __init__(self, encoded_private: str) -> None:
        raw = decode(encoded_private, "SINAN_EXECUTOR_PRIVATE_KEY")
        self._key = X25519PrivateKey.from_private_bytes(raw)

    def public_key(self) -> str:
        """The matching public key, encoded, to check against the API's."""
        return encode(public_bytes(self._key.public_key()))

    def open(self, blob: bytes, account_id: uuid.UUID, purpose: str) -> str:
        if blob[:1] != VERSION or len(blob) <= HEADER_SIZE:
            raise DecryptError("unknown sealed token format")
        eph_raw = blob[1 : 1 + KEY_SIZE]
        nonce = blob[1 + KEY_SIZE : HEADER_SIZE]
        sealed = blob[HEADER_SIZE:]
        ephemeral = X25519PublicKey.from_public_bytes(eph_raw)
        try:
            shared = self._key.exchange(ephemeral)
        except ValueError as exc:
            # A low-order ephemeral key gives an all-zero secret; refuse it
            raise DecryptError("sealed token has an invalid ephemeral key") from exc
        own = public_bytes(self._key.public_key())
        aead = derive(shared, eph_raw, own)
        aad = bind(account_id, purpose)
        try:
            data = aead.decrypt(nonce, sealed, aad)
        except InvalidTag as exc:
            raise DecryptError("sealed token failed authentication") from exc
        return data.decode("utf-8")


if __name__ == "__main__":
    # Prints a fresh key pair. Run it yourself and put both lines in your own
    # .env: the API reads the public key, only the Executor the private one.
    private, public = generate_pair()
    print(f"SINAN_EXECUTOR_PRIVATE_KEY={private}")
    print(f"SINAN_EXECUTOR_PUBLIC_KEY={public}")
