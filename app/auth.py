"""Single-user login for the API and Dashboard.

Passwords are hashed with scrypt from the standard library. The stored hash
has no `$` characters, so it can sit unquoted in a Compose .env file without
being read as a variable.

Sessions are stateless signed tokens: `v1.<expires>.<nonce>.<signature>`.
The signing key is derived from the session secret and the password hash, so
changing either one ends every existing session.
"""

import base64
import binascii
import getpass
import hashlib
import hmac
import os
import sys
import time
from collections import deque
from collections.abc import Callable

SCHEME = "scrypt"
# OWASP minimum for scrypt: N=2^17, r=8, p=1 (about 128 MiB per hash)
COST_N = 2**17
COST_R = 8
COST_P = 1
SALT_SIZE = 16
HASH_SIZE = 32
MAX_MEMORY = 256 * 1024 * 1024

TOKEN_VERSION = "v1"  # noqa: S105 - a format tag, not a secret
SESSION_TTL = 12 * 60 * 60
SECRET_SIZE = 32
MIN_SECRET_LENGTH = 32

Clock = Callable[[], float]


class InvalidHashError(ValueError):
    """The configured password hash is malformed."""


def b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    data = password.encode("utf-8")
    return hashlib.scrypt(data, salt=salt, n=n, r=r, p=p, maxmem=MAX_MEMORY, dklen=HASH_SIZE)


def hash_password(password: str, n: int = COST_N, r: int = COST_R, p: int = COST_P) -> str:
    """Return `scrypt:N:r:p:salt:hash` for SINAN_DASHBOARD_PASSWORD_HASH."""
    salt = os.urandom(SALT_SIZE)
    digest = scrypt(password, salt, n, r, p)
    parts = [SCHEME, str(n), str(r), str(p), b64encode(salt), b64encode(digest)]
    return ":".join(parts)


def parse_hash(encoded: str) -> tuple[int, int, int, bytes, bytes]:
    """Split a stored hash into its cost parameters, salt and digest."""
    parts = encoded.split(":")
    if len(parts) != 6 or parts[0] != SCHEME:
        raise InvalidHashError("password hash must look like scrypt:N:r:p:salt:hash")
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = b64decode(parts[4])
        digest = b64decode(parts[5])
    except (ValueError, binascii.Error) as exc:
        raise InvalidHashError("password hash has a malformed field") from exc
    return n, r, p, salt, digest


def verify_password(password: str, encoded: str) -> bool:
    n, r, p, salt, digest = parse_hash(encoded)
    candidate = scrypt(password, salt, n, r, p)
    return hmac.compare_digest(candidate, digest)


def generate_secret() -> str:
    """Return a new random value for SINAN_SESSION_SECRET."""
    raw = os.urandom(SECRET_SIZE)
    return b64encode(raw)


class SessionSigner:
    """Issues and checks signed session tokens."""

    def __init__(self, secret: str, password_hash: str, ttl: int = SESSION_TTL) -> None:
        label = b"sinan-session:" + password_hash.encode("utf-8")
        self._key = hmac.digest(secret.encode("utf-8"), label, "sha256")
        self.ttl = ttl

    def _sign(self, body: str) -> str:
        mac = hmac.digest(self._key, body.encode("ascii"), "sha256")
        return b64encode(mac)

    def issue(self, now: float) -> str:
        expires = int(now) + self.ttl
        nonce = b64encode(os.urandom(12))
        body = f"{TOKEN_VERSION}.{expires}.{nonce}"
        return f"{body}.{self._sign(body)}"

    def check(self, token: str, now: float) -> bool:
        """True only for an untampered token from this key that has not expired."""
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != TOKEN_VERSION:
            return False
        body = ".".join(parts[:3])
        try:
            expected = self._sign(body)
        except UnicodeEncodeError:
            return False
        given = parts[3].encode("utf-8")
        if not hmac.compare_digest(expected.encode("ascii"), given):
            return False
        try:
            expires = int(parts[1])
        except ValueError:
            return False
        return now < expires


class LoginLimiter:
    """Caps failed logins across all clients.

    There is one user and every request arrives through the Dashboard, so the
    limit is global rather than per address. While locked, even the correct
    password is refused, so guessing gains nothing.
    """

    def __init__(self, max_failures: int = 5, window: float = 300.0) -> None:
        self.max_failures = max_failures
        self.window = window
        self._failures: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self._failures and now - self._failures[0] >= self.window:
            self._failures.popleft()

    def retry_after(self, now: float) -> int:
        """Seconds until a login may be tried again; 0 when not locked."""
        self._prune(now)
        if len(self._failures) < self.max_failures:
            return 0
        wait = self._failures[0] + self.window - now
        return max(1, int(wait) + 1)

    def fail(self, now: float) -> None:
        self._failures.append(now)

    def reset(self) -> None:
        self._failures.clear()


class Auth:
    """Everything the API needs to log in and check sessions."""

    def __init__(
        self,
        password_hash: str,
        signer: SessionSigner,
        limiter: LoginLimiter,
        clock: Clock = time.time,
    ) -> None:
        self.password_hash = password_hash
        self.signer = signer
        self.limiter = limiter
        self.clock = clock


def prompt_hash() -> str:
    first = getpass.getpass("Dashboard password: ")
    second = getpass.getpass("Repeat: ")
    if first != second:
        raise SystemExit("Passwords do not match")
    if len(first) < 12:
        raise SystemExit("Use at least 12 characters")
    return hash_password(first)


def main(args: list[str]) -> None:
    # Run these yourself and put the output in your own .env
    if args == ["hash"]:
        print(prompt_hash())
    elif args == ["secret"]:
        print(generate_secret())
    else:
        raise SystemExit("usage: python -m app.auth hash|secret")


if __name__ == "__main__":
    main(sys.argv[1:])
