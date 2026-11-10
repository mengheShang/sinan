"""OAuth interface for connecting a mailbox with read-only access.

Only `gmail.readonly` is ever requested here. Write and Calendar scopes come
in M3 and will get their own flow, held by the Executor.
"""

import base64
import hashlib
from dataclasses import dataclass
from typing import Protocol

from pydantic import SecretStr

# The one scope this flow may request. tests/test_oauth.py fails if it changes.
READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def pkce_challenge(verifier: str) -> str:
    """S256 code challenge for a PKCE verifier (RFC 7636, section 4.2)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii")
    return encoded.rstrip("=")


class OAuthError(Exception):
    """The provider refused the request. The message never holds a token or code."""


class ReauthRequiredError(OAuthError):
    """The refresh token expired or was revoked (`invalid_grant`)."""


@dataclass(frozen=True)
class TokenGrant:
    refresh_token: SecretStr
    access_token: SecretStr
    # Scopes the user actually granted; they may uncheck some on the consent screen
    scopes: frozenset[str]


class OAuthClient(Protocol):
    def authorize_url(self, state: str, challenge: str) -> str:
        """Return the consent page URL for a PKCE (S256) authorization request."""
        ...

    def exchange(self, code: str, verifier: str) -> TokenGrant:
        """Trade an authorization code for tokens. Raises OAuthError."""
        ...

    def refresh(self, refresh_token: SecretStr) -> SecretStr:
        """Return a fresh access token. Raises ReauthRequiredError on invalid_grant."""
        ...

    def mailbox_email(self, access_token: SecretStr) -> str:
        """Return the address of the authorized mailbox. Raises OAuthError."""
        ...
