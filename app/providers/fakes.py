"""In-memory fakes for tests and demo mode. They never touch the network."""

from dataclasses import replace
from datetime import datetime
from urllib.parse import urlencode

from pydantic import SecretStr

from app.providers.mail import (
    HistoryChanges,
    HistoryExpiredError,
    MailMessage,
    MailProfile,
    MessageNotFoundError,
)
from app.providers.oauth import (
    READ_SCOPE,
    OAuthError,
    ReauthRequiredError,
    TokenGrant,
    pkce_challenge,
)

EMPTY_RESULT = '{"items": []}'
NO_LINK = '{"target": null}'


class FakeMailReader:
    """A mailbox held in memory.

    Each message gets the next history ID when it is added, starting at 1.
    Call `expire_history_before` to make older cursors fail like Gmail's 404.
    """

    def __init__(self, email: str, messages: list[MailMessage] | None = None) -> None:
        self.email = email
        self._messages: list[MailMessage] = []
        self._oldest_valid = 0
        for msg in messages or []:
            self.add(msg)

    def add(self, msg: MailMessage) -> None:
        self._messages.append(msg)

    def expire_history_before(self, history_id: str) -> None:
        self._oldest_valid = int(history_id)

    def get_profile(self) -> MailProfile:
        current = str(len(self._messages))
        return MailProfile(email=self.email, history_id=current)

    def list_history(self, start_history_id: str) -> HistoryChanges:
        start = int(start_history_id)
        if start < self._oldest_valid:
            raise HistoryExpiredError(start_history_id)
        newer = self._messages[start:]
        ids = [m.provider_msg_id for m in newer]
        current = str(len(self._messages))
        return HistoryChanges(added_ids=ids, history_id=current)

    def list_message_ids(self, since: datetime) -> list[str]:
        recent = [m for m in self._messages if m.received_at >= since]
        recent.sort(key=lambda m: m.received_at)
        return [m.provider_msg_id for m in recent]

    def get_message(self, provider_msg_id: str) -> MailMessage:
        for msg in self._messages:
            if msg.provider_msg_id == provider_msg_id:
                return replace(msg)
        raise MessageNotFoundError(provider_msg_id)


class FakeExtractor:
    """Returns canned JSON. A response is picked by a marker found in the text."""

    model = "fake-extractor"

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[str] = []

    def extract(self, prompt: str, text: str) -> str:
        self.calls.append(text)
        for marker, result in self._responses.items():
            if marker in text:
                return result
        return EMPTY_RESULT


class FakeLinkPicker:
    """Returns canned link replies, picked by a marker found in the text.

    With no marker found it answers "no link", so the demo never links a
    change by the model.
    """

    model = "fake-link-picker"

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[str] = []

    def pick(self, prompt: str, text: str) -> str:
        self.calls.append(text)
        for marker, result in self._responses.items():
            if marker in text:
                return result
        return NO_LINK


class FakeOAuth:
    """A stand-in for Google's consent screen and token endpoint.

    `authorize_url` points straight back at the callback with a code, so the
    demo can run the whole connect flow without a Google account. The fake
    checks PKCE like the real endpoint: the verifier must match the challenge.
    """

    def __init__(
        self,
        email: str,
        redirect_uri: str,
        scopes: frozenset[str] = frozenset({READ_SCOPE}),
    ) -> None:
        self.email = email
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self._challenges: dict[str, str] = {}
        self._issued = 0
        self.revoked: set[str] = set()

    def authorize_url(self, state: str, challenge: str) -> str:
        code = f"fake-code-{len(self._challenges)}"
        self._challenges[code] = challenge
        query = urlencode({"state": state, "code": code})
        return f"{self.redirect_uri}?{query}"

    def exchange(self, code: str, verifier: str) -> TokenGrant:
        challenge = self._challenges.pop(code, None)
        if challenge is None or pkce_challenge(verifier) != challenge:
            raise OAuthError("invalid_grant")
        self._issued += 1
        refresh = SecretStr(f"fake-refresh-{self._issued}")
        access = SecretStr(f"fake-access-{self._issued}")
        return TokenGrant(refresh, access, self.scopes)

    def refresh(self, refresh_token: SecretStr) -> SecretStr:
        value = refresh_token.get_secret_value()
        if value in self.revoked or not value.startswith("fake-refresh-"):
            raise ReauthRequiredError("invalid_grant")
        return SecretStr("fake-access-refreshed")

    def mailbox_email(self, access_token: SecretStr) -> str:
        return self.email
