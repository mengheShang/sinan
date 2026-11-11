"""Read-only mailbox interface.

Every mailbox adapter implements `MailReader`. The interface is read-only on
purpose: it has no send, delete, trash, draft, label or settings methods, and
none may be added (CLAUDE.md, rule 4). Writes belong to the Executor in M3.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class HistoryExpiredError(Exception):
    """The history cursor is too old. The caller must re-sync a time window."""


class MessageNotFoundError(Exception):
    """The provider has no message with this ID."""


class MailProviderError(Exception):
    """A provider request failed. Its text is a short safe code, such as
    "http_403:userRateLimitExceeded", never mail content, so it may be logged."""


@dataclass(frozen=True)
class MailProfile:
    email: str
    # Current history cursor, used as the starting point for incremental sync
    history_id: str


@dataclass(frozen=True)
class HistoryChanges:
    # IDs of messages added since the cursor, oldest first, without duplicates
    added_ids: list[str]
    # Cursor to store after the changes are saved
    history_id: str


@dataclass(frozen=True)
class MailMessage:
    provider_msg_id: str
    thread_id: str | None
    received_at: datetime
    sent_at: datetime | None
    from_addr: str | None
    subject: str | None
    body_text: str | None
    headers: dict[str, str] = field(default_factory=dict)
    label_ids: list[str] = field(default_factory=list)


class MailReader(Protocol):
    """Reads one mailbox. Implementations must never write to it."""

    def get_profile(self) -> MailProfile:
        """Return the mailbox address and its current history cursor."""
        ...

    def list_history(self, start_history_id: str) -> HistoryChanges:
        """Return messages added since the cursor.

        Raises HistoryExpiredError when the cursor is too old.
        """
        ...

    def list_message_ids(self, since: datetime) -> list[str]:
        """Return IDs of messages received at or after `since`, oldest first."""
        ...

    def get_message(self, provider_msg_id: str) -> MailMessage:
        """Return one message. Raises MessageNotFoundError if it does not exist."""
        ...
