"""Turn a Gmail API message resource into a MailMessage. Pure functions only.

Gmail returns MIME parts with base64url bodies. Sinan keeps a short list of
headers, the first plain-text body (or text pulled from HTML when there is
none), and the times in UTC.
"""

import base64
import binascii
from datetime import UTC, datetime
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from app.providers.mail import MailMessage

# Headers the prefilter and extraction may need; everything else is dropped
KEPT_HEADERS = (
    "From",
    "To",
    "Cc",
    "Reply-To",
    "Subject",
    "Date",
    "Message-ID",
    "List-Id",
    "List-Unsubscribe",
    "Precedence",
    "Auto-Submitted",
)
MAX_HEADER = 1000
MAX_BODY = 100_000

# Labels that mean the message is not incoming mail worth reading
SKIP_LABELS = frozenset({"SPAM", "TRASH", "DRAFT"})


def wanted(label_ids: list[str]) -> bool:
    """All mail except spam, trash, drafts and your own sent mail.

    Mail you sent to yourself carries both SENT and INBOX, and is kept.
    """
    labels = set(label_ids)
    if labels & SKIP_LABELS:
        return False
    return not ("SENT" in labels and "INBOX" not in labels)


def header_map(payload: dict[str, Any]) -> dict[str, str]:
    kept = {name.lower(): name for name in KEPT_HEADERS}
    out: dict[str, str] = {}
    for item in payload.get("headers", []):
        name = str(item.get("name", ""))
        canonical = kept.get(name.lower())
        if canonical is None or canonical in out:
            continue
        out[canonical] = str(item.get("value", ""))[:MAX_HEADER]
    return out


def decode_body(data: str, content_type: str) -> str:
    padding = "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode(data + padding)
    except (binascii.Error, ValueError):
        return ""
    probe = Message()
    probe["Content-Type"] = content_type or "text/plain"
    charset = probe.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def walk(part: dict[str, Any]) -> list[dict[str, Any]]:
    """Every MIME part in document order, depth first."""
    parts = [part]
    for child in part.get("parts", []) or []:
        parts.extend(walk(child))
    return parts


def first_body(payload: dict[str, Any], mime: str) -> str | None:
    for part in walk(payload):
        if part.get("mimeType") != mime or part.get("filename"):
            continue
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        content_type = header_map_raw(part).get("content-type", mime)
        return decode_body(data, content_type)
    return None


def header_map_raw(part: dict[str, Any]) -> dict[str, str]:
    headers = part.get("headers", []) or []
    return {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in headers}


class TextFromHtml(HTMLParser):
    BLOCKS = frozenset({"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "table"})
    HIDDEN = frozenset({"script", "style", "head"})

    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.HIDDEN:
            self._hidden += 1
        elif tag in self.BLOCKS:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.HIDDEN and self._hidden:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.chunks.append(data)


def html_to_text(html: str) -> str:
    parser = TextFromHtml()
    parser.feed(html)
    parser.close()
    lines = "".join(parser.chunks).splitlines()
    kept = [" ".join(line.split()) for line in lines]
    return "\n".join(line for line in kept if line)


def body_text(payload: dict[str, Any]) -> str | None:
    plain = first_body(payload, "text/plain")
    if plain is None:
        html = first_body(payload, "text/html")
        plain = html_to_text(html) if html is not None else None
    if plain is None:
        return None
    return plain[:MAX_BODY]


def parse_date(value: str | None) -> datetime | None:
    """The Date header in UTC, or None when missing, invalid or without a zone."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def parse_message(resource: dict[str, Any]) -> MailMessage:
    payload = resource.get("payload") or {}
    headers = header_map(payload)
    internal_ms = int(resource.get("internalDate", "0"))
    received = datetime.fromtimestamp(internal_ms / 1000, tz=UTC)
    sender = parseaddr(headers.get("From", ""))[1].lower() or None
    return MailMessage(
        provider_msg_id=str(resource["id"]),
        thread_id=resource.get("threadId"),
        received_at=received,
        sent_at=parse_date(headers.get("Date")),
        from_addr=sender,
        subject=headers.get("Subject"),
        body_text=body_text(payload),
        headers=headers,
        label_ids=list(resource.get("labelIds", [])),
    )
