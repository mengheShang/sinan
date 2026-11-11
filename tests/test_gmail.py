"""Read-only Gmail client and message parsing, against a fake Gmail endpoint."""

import base64
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from app.providers.gmail import (
    HISTORY_URL,
    MESSAGES_URL,
    PROFILE_URL,
    GmailError,
    GmailReader,
)
from app.providers.gmail_parse import MAX_BODY, html_to_text, parse_date, parse_message, wanted
from app.providers.mail import HistoryExpiredError, MessageNotFoundError

Handler = Callable[[httpx.Request], httpx.Response]


def b64(text: str, charset: str = "utf-8") -> str:
    return base64.urlsafe_b64encode(text.encode(charset)).decode().rstrip("=")


def ok(body: object) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(body).encode())


class Recorder:
    """A fake Gmail that remembers every request it receives."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def reader(self) -> GmailReader:
        http = httpx.Client(transport=httpx.MockTransport(self))
        return GmailReader(SecretStr("fake-access"), http)


def resource(msg_id: str, labels: list[str], payload: Mapping[str, object]) -> dict[str, Any]:
    return {
        "id": msg_id,
        "threadId": "t1",
        "labelIds": labels,
        "internalDate": "1794400000000",
        "payload": payload,
    }


PLAIN = {
    "mimeType": "multipart/alternative",
    "headers": [
        {"name": "From", "value": "Prof. Rivera <Rivera@example.edu>"},
        {"name": "Subject", "value": "CS 000: Homework 4"},
        {"name": "Date", "value": "Tue, 10 Nov 2026 09:00:00 -0800"},
        {"name": "X-Tracking-Id", "value": "dropped"},
        {"name": "List-Id", "value": "<cs000.example.edu>"},
    ],
    "parts": [
        {"mimeType": "text/plain", "body": {"data": b64("Homework 4 is due Nov 20.")}},
        {"mimeType": "text/html", "body": {"data": b64("<p>ignored html</p>")}},
    ],
}


# Every request is a read


def test_reader_only_sends_gets_to_read_endpoints() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        if url == PROFILE_URL:
            return ok({"emailAddress": "student@example.edu", "historyId": "9"})
        if url == HISTORY_URL:
            return ok({"history": [], "historyId": "9"})
        if url == MESSAGES_URL:
            return ok({"messages": [{"id": "a1"}]})
        return ok(resource("a1", ["INBOX"], PLAIN))

    fake = Recorder(handle)
    reader = fake.reader()
    reader.get_profile()
    reader.list_history("1")
    reader.list_message_ids(datetime(2026, 11, 1, tzinfo=UTC))
    reader.get_message("a1")
    allowed = {PROFILE_URL, HISTORY_URL, MESSAGES_URL, f"{MESSAGES_URL}/a1"}
    for request in fake.requests:
        assert request.method == "GET"
        assert str(request.url).split("?")[0] in allowed
        assert request.headers["authorization"] == "Bearer fake-access"


def test_odd_message_ids_never_reach_the_url() -> None:
    fake = Recorder(lambda r: ok({}))
    with pytest.raises(MessageNotFoundError):
        fake.reader().get_message("../a1/trash")
    assert fake.requests == []


# Endpoints


def test_profile() -> None:
    fake = Recorder(lambda r: ok({"emailAddress": "student@example.edu", "historyId": "42"}))
    profile = fake.reader().get_profile()
    assert profile.email == "student@example.edu"
    assert profile.history_id == "42"


def test_history_pages_and_dedupes() -> None:
    pages = {
        None: {
            "history": [{"messagesAdded": [{"message": {"id": "a"}}, {"message": {"id": "b"}}]}],
            "nextPageToken": "p2",
            "historyId": "20",
        },
        "p2": {
            "history": [{"messagesAdded": [{"message": {"id": "b"}}, {"message": {"id": "c"}}]}],
            "historyId": "21",
        },
    }

    def handle(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlsplit(str(request.url)).query)
        assert query["startHistoryId"] == ["10"]
        assert query["historyTypes"] == ["messageAdded"]
        token = query.get("pageToken", [None])[0]
        return ok(pages[token])

    changes = Recorder(handle).reader().list_history("10")
    assert changes.added_ids == ["a", "b", "c"]
    assert changes.history_id == "21"


def test_history_404_means_expired_cursor() -> None:
    fake = Recorder(lambda r: httpx.Response(404, content=b"{}"))
    with pytest.raises(HistoryExpiredError):
        fake.reader().list_history("1")


def test_window_query_and_order() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlsplit(str(request.url)).query)
        q = query["q"][0]
        assert q.startswith("after:1793491200 ")
        for part in ("-in:spam", "-in:trash", "-in:drafts", "-in:sent"):
            assert part in q
        if "pageToken" in query:
            return ok({"messages": [{"id": "old"}]})
        return ok({"messages": [{"id": "new"}, {"id": "mid"}], "nextPageToken": "p2"})

    since = datetime(2026, 11, 1, tzinfo=UTC)
    ids = Recorder(handle).reader().list_message_ids(since)
    assert ids == ["old", "mid", "new"]


def test_message_404() -> None:
    fake = Recorder(lambda r: httpx.Response(404, content=b"{}"))
    with pytest.raises(MessageNotFoundError):
        fake.reader().get_message("a1")


def test_errors_hold_status_only() -> None:
    fake = Recorder(lambda r: httpx.Response(500, content=b"secret body text"))
    with pytest.raises(GmailError) as caught:
        fake.reader().get_profile()
    assert str(caught.value) == "http_500"


def google_error(status: int, reason: str, headers: dict[str, str] | None = None) -> httpx.Response:
    """An error in Google's JSON format, with a message that must never be kept."""
    body = {
        "error": {
            "code": status,
            "message": "Quota text that might echo request details",
            "errors": [{"reason": reason, "message": "details"}],
        }
    }
    return httpx.Response(status, content=json.dumps(body).encode(), headers=headers)


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def flaky(failures: list[httpx.Response]) -> Handler:
    """Answers with each failure in turn, then succeeds."""

    def handle(request: httpx.Request) -> httpx.Response:
        if failures:
            return failures.pop(0)
        return ok({"emailAddress": "student@example.edu", "historyId": "42"})

    return handle


def reader_with(fake: Recorder, sleeps: Sleeps) -> GmailReader:
    http = httpx.Client(transport=httpx.MockTransport(fake))
    return GmailReader(SecretStr("fake-access"), http, sleep=sleeps)


@pytest.mark.parametrize("reason", ["userRateLimitExceeded", "rateLimitExceeded"])
def test_rate_limited_403_is_retried(reason: str) -> None:
    fake = Recorder(flaky([google_error(403, reason), google_error(403, reason)]))
    sleeps = Sleeps()
    profile = reader_with(fake, sleeps).get_profile()
    assert profile.history_id == "42"
    assert len(fake.requests) == 3
    assert sleeps.waits == [1.0, 2.0]


def test_429_honors_retry_after_with_a_cap() -> None:
    failures = [
        httpx.Response(429, content=b"{}", headers={"retry-after": "3"}),
        httpx.Response(429, content=b"{}", headers={"retry-after": "600"}),
    ]
    sleeps = Sleeps()
    reader_with(Recorder(flaky(failures)), sleeps).get_profile()
    assert sleeps.waits == [3.0, 32.0]


def test_rate_limit_gives_up_after_the_backoff() -> None:
    fake = Recorder(lambda r: google_error(403, "userRateLimitExceeded"))
    sleeps = Sleeps()
    with pytest.raises(GmailError) as caught:
        reader_with(fake, sleeps).get_message("a1")
    assert str(caught.value) == "http_403:userRateLimitExceeded"
    assert sleeps.waits == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert len(fake.requests) == 6


def test_other_403_fails_at_once_with_its_reason() -> None:
    fake = Recorder(lambda r: google_error(403, "domainPolicy"))
    sleeps = Sleeps()
    with pytest.raises(GmailError) as caught:
        reader_with(fake, sleeps).get_message("a1")
    assert str(caught.value) == "http_403:domainPolicy"
    assert sleeps.waits == []
    assert len(fake.requests) == 1


def test_unsafe_reason_is_dropped() -> None:
    fake = Recorder(lambda r: google_error(403, "not a code: Homework 3"))
    with pytest.raises(GmailError) as caught:
        reader_with(fake, Sleeps()).get_profile()
    assert str(caught.value) == "http_403"


def test_network_failure() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(GmailError, match="unreachable"):
        Recorder(handle).reader().get_profile()


# Parsing


def test_parse_plain_message() -> None:
    msg = parse_message(resource("a1", ["INBOX", "UNREAD"], PLAIN))
    assert msg.provider_msg_id == "a1"
    assert msg.from_addr == "rivera@example.edu"
    assert msg.subject == "CS 000: Homework 4"
    assert msg.body_text == "Homework 4 is due Nov 20."
    assert msg.sent_at == datetime(2026, 11, 10, 17, 0, tzinfo=UTC)
    assert msg.received_at == datetime.fromtimestamp(1794400000, tz=UTC)
    assert "X-Tracking-Id" not in msg.headers
    assert msg.headers["List-Id"] == "<cs000.example.edu>"
    assert msg.label_ids == ["INBOX", "UNREAD"]


def test_html_only_message_becomes_text() -> None:
    html = "<html><head><style>p{}</style></head><body><p>Quiz  on</p><p>Friday</p>"
    html += "<script>alert(1)</script></body></html>"
    payload = {"mimeType": "text/html", "headers": [], "body": {"data": b64(html)}}
    msg = parse_message(resource("a2", ["INBOX"], payload))
    assert msg.body_text == "Quiz on\nFriday"


def test_charset_is_respected() -> None:
    part = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": "text/plain; charset=iso-8859-1"}],
        "body": {"data": b64("café", "iso-8859-1")},
    }
    msg = parse_message(resource("a3", ["INBOX"], part))
    assert msg.body_text == "café"


def test_attachments_are_not_bodies() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "headers": [],
        "parts": [
            {"mimeType": "text/plain", "filename": "notes.txt", "body": {"data": b64("file")}},
            {"mimeType": "text/plain", "filename": "", "body": {"data": b64("the body")}},
        ],
    }
    assert parse_message(resource("a4", ["INBOX"], payload)).body_text == "the body"


def test_body_is_capped() -> None:
    payload = {"mimeType": "text/plain", "headers": [], "body": {"data": b64("x" * (MAX_BODY + 5))}}
    body = parse_message(resource("a5", ["INBOX"], payload)).body_text
    assert body is not None
    assert len(body) == MAX_BODY


def test_no_body() -> None:
    payload = {"mimeType": "image/png", "headers": [], "body": {"data": b64("png")}}
    assert parse_message(resource("a6", ["INBOX"], payload)).body_text is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Tue, 10 Nov 2026 09:00:00 -0800", datetime(2026, 11, 10, 17, 0, tzinfo=UTC)),
        ("Tue, 10 Nov 2026 09:00:00", None),
        ("not a date", None),
        (None, None),
    ],
)
def test_parse_date(value: str | None, expected: datetime | None) -> None:
    assert parse_date(value) == expected


def test_html_to_text_skips_hidden() -> None:
    assert html_to_text("<style>x</style><div>a</div><script>y</script>") == "a"


@pytest.mark.parametrize(
    ("labels", "keep"),
    [
        (["INBOX"], True),
        (["CATEGORY_UPDATES"], True),
        (["Label_7"], True),
        (["SPAM"], False),
        (["TRASH", "INBOX"], False),
        (["DRAFT"], False),
        (["SENT"], False),
        (["SENT", "INBOX"], True),
    ],
)
def test_wanted(labels: list[str], keep: bool) -> None:
    assert wanted(labels) is keep
