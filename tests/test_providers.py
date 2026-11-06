import inspect
import json
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.crypto import generate_key
from app.providers import factory
from app.providers.demo_data import DEMO_MESSAGES, DEMO_RESPONSES
from app.providers.fakes import FakeExtractor, FakeMailReader
from app.providers.mail import (
    HistoryExpiredError,
    MailMessage,
    MailReader,
    MessageNotFoundError,
)

# Words that must never appear in a mailbox reader's method names
FORBIDDEN = ("send", "delete", "trash", "draft", "setting", "modify", "label", "insert")


def public_methods(cls: type) -> list[str]:
    members = inspect.getmembers(cls, callable)
    return [name for name, _ in members if not name.startswith("_")]


def msg(n: int, day: int) -> MailMessage:
    when = datetime(2026, 11, day, tzinfo=UTC)
    return MailMessage(
        provider_msg_id=f"m{n}",
        thread_id=None,
        received_at=when,
        sent_at=when,
        from_addr="a@example.com",
        subject="s",
        body_text="b",
    )


@pytest.mark.parametrize("cls", [MailReader, FakeMailReader])
def test_mail_reader_has_no_write_methods(cls: type) -> None:
    for name in public_methods(cls):
        lowered = name.lower()
        hits = [word for word in FORBIDDEN if word in lowered]
        assert not hits, f"{cls.__name__}.{name} looks like a write method"


def test_fake_satisfies_protocol() -> None:
    reader: MailReader = FakeMailReader("a@example.com")
    assert reader.get_profile().history_id == "0"


def test_history_returns_only_new_messages() -> None:
    reader = FakeMailReader("a@example.com", [msg(1, 1), msg(2, 2)])
    start = reader.get_profile().history_id
    reader.add(msg(3, 3))
    changes = reader.list_history(start)
    assert changes.added_ids == ["m3"]
    assert changes.history_id == "3"


def test_history_with_no_changes() -> None:
    reader = FakeMailReader("a@example.com", [msg(1, 1)])
    changes = reader.list_history("1")
    assert changes.added_ids == []
    assert changes.history_id == "1"


def test_expired_history_raises() -> None:
    reader = FakeMailReader("a@example.com", [msg(1, 1), msg(2, 2)])
    reader.expire_history_before("2")
    with pytest.raises(HistoryExpiredError):
        reader.list_history("1")
    assert reader.list_history("2").added_ids == []


def test_list_message_ids_since() -> None:
    reader = FakeMailReader("a@example.com", [msg(2, 5), msg(1, 1), msg(3, 9)])
    since = datetime(2026, 11, 4, tzinfo=UTC)
    assert reader.list_message_ids(since) == ["m2", "m3"]


def test_get_message() -> None:
    reader = FakeMailReader("a@example.com", [msg(1, 1)])
    assert reader.get_message("m1").provider_msg_id == "m1"
    with pytest.raises(MessageNotFoundError):
        reader.get_message("missing")


def test_fake_extractor_picks_response_by_marker() -> None:
    ext = FakeExtractor({"needle": '{"items": [1]}'})
    assert ext.extract("p", "has a needle") == '{"items": [1]}'
    assert ext.extract("p", "nothing here") == '{"items": []}'
    assert ext.calls == ["has a needle", "nothing here"]


def test_demo_data_is_fictional() -> None:
    domains = ("@example.edu", "@example.com")
    for m in DEMO_MESSAGES:
        assert m.from_addr is not None
        assert m.from_addr.endswith(domains)


def test_demo_evidence_appears_verbatim() -> None:
    reader = FakeMailReader("student@example.edu", DEMO_MESSAGES)
    ext = FakeExtractor(DEMO_RESPONSES)
    found = 0
    for msg_id in reader.list_message_ids(datetime(2026, 1, 1, tzinfo=UTC)):
        body = reader.get_message(msg_id).body_text or ""
        items = json.loads(ext.extract("p", body))["items"]
        for item in items:
            assert item["evidence"] in body
            assert item["due_raw"] in item["evidence"]
            found += 1
    assert found == len(DEMO_RESPONSES)


def test_demo_injection_yields_no_items() -> None:
    ext = FakeExtractor(DEMO_RESPONSES)
    injection = next(m for m in DEMO_MESSAGES if "ignore all previous" in (m.body_text or ""))
    result = json.loads(ext.extract("p", injection.body_text or ""))
    assert result["items"] == []


def test_factory_uses_fakes_in_demo_mode() -> None:
    settings = Settings(sinan_mode="demo")
    assert isinstance(factory.mail_reader(settings), FakeMailReader)
    assert isinstance(factory.extractor(settings), FakeExtractor)


def test_factory_live_mode_is_not_wired_yet() -> None:
    key = SecretStr(generate_key())
    settings = Settings(sinan_mode="live", sinan_encryption_key=key)
    with pytest.raises(NotImplementedError):
        factory.mail_reader(settings)
    with pytest.raises(NotImplementedError):
        factory.extractor(settings)
