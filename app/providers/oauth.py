"""OAuth interface: read, write and calendar authorization (ADR 0003, ADR 0021).

Each flow requests exactly one set of scopes:

- read: `gmail.readonly`, held by the Worker.
- write: `gmail.modify`, sealed for the Executor (ADR 0021, decision 2).
- calendar: `calendar.events`, plus `openid` and `email` only so the callback
  can check which Google account granted it (owner, 2026-11-19). Sealed for
  the Executor too.
"""

import base64
import hashlib
from dataclasses import dataclass
from typing import Protocol

from pydantic import SecretStr

# The one scope this flow may request. tests/test_oauth.py fails if it changes.
READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
WRITE_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
# Asked as "openid email"; Google reports "email" by its full name
OPENID = "openid"
EMAIL_ASKED = "email"
EMAIL_GRANTED = "https://www.googleapis.com/auth/userinfo.email"


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
    def authorize_url(
        self,
        state: str,
        challenge: str,
        scopes: tuple[str, ...] = (READ_SCOPE,),
        login_hint: str | None = None,
    ) -> str:
        """Return the consent page URL for a PKCE (S256) authorization request.

        `login_hint` suggests the Google account; the callback still checks it.
        """
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

    def account_email(self, access_token: SecretStr) -> str:
        """Return the verified address of the authorized Google account, from
        the OpenID userinfo endpoint (needs `openid email`). Raises OAuthError."""
        ...
