"""The evaluation set format: the shipped synthetic set loads, and bad samples fail safely."""

import copy
import json
from typing import Any

import pytest

from evals.dataset import SetError, load, load_lines

BODY = "Homework 9 is due Friday Nov 13 at 11:59pm PT. Secret marker text."
FICTIONAL = ("example.edu", "example.com", "example.org", "example.net")


def sample(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "t-001",
        "input": {
            "from_addr": "rivera@example.edu",
            "subject": "CS 000 Homework 9",
            "body_text": BODY,
            "sent_at": "2026-11-02T17:00:00Z",
            "role": "school",
            "headers": {},
        },
        "labels": {
            "items": [
                {
                    "ref": "hw9",
                    "kind": "deadline",
                    "course": "CS 000",
                    "title": "Homework 9",
                    "due_raw": "Friday Nov 13 at 11:59pm PT",
                    "due_at": "2026-11-14T07:59:00Z",
                    "due_ambiguous": False,
                    "changes": None,
                }
            ],
            "safe_to_auto_read": False,
        },
        "tags": ["course"],
    }
    base.update(over)
    return base


def edited(path: str, value: Any) -> dict[str, Any]:
    """A valid sample with one field set (or removed, if value is ...)."""
    data = sample()
    *parents, last = path.split(".")
    node: Any = data
    for part in parents:
        node = node[int(part)] if part.isdigit() else node[part]
    if value is ...:
        del node[last]
    else:
        node[last] = value
    return data


def lines(*samples: dict[str, Any]) -> list[str]:
    return [json.dumps(s) for s in samples]


def rejected(*samples: dict[str, Any]) -> str:
    with pytest.raises(SetError) as info:
        load_lines(lines(*samples))
    message = str(info.value)
    assert "Secret marker" not in message
    assert "rivera" not in message
    return message


def test_synthetic_set_loads() -> None:
    samples = load()
    assert len(samples) >= 50
    assert any(s.input.role == "personal" for s in samples)
    assert any(not s.has_item for s in samples)
    items = [i for s in samples for i in s.labels.items]
    assert any(i.changes for i in items)
    assert any(i.due_ambiguous for i in items)
    assert any(i.kind == "todo" for i in items)


def test_synthetic_senders_are_fictional() -> None:
    for s in load():
        domain = s.input.from_addr.rsplit("@", 1)[1]
        assert domain.endswith(FICTIONAL), s.id


def test_valid_sample_loads() -> None:
    [loaded] = load_lines(lines(sample()))
    assert loaded.has_item
    assert loaded.labels.items[0].due_at is not None


def test_blank_lines_are_skipped() -> None:
    loaded = load_lines(["", *lines(sample()), "  "])
    assert len(loaded) == 1


def test_empty_set_is_rejected() -> None:
    with pytest.raises(SetError, match="empty"):
        load_lines([])


def test_missing_field_names_the_field() -> None:
    message = rejected(edited("labels.safe_to_auto_read", ...))
    assert "line 1" in message
    assert "labels.safe_to_auto_read" in message


def test_unknown_field_names_the_field() -> None:
    message = rejected(edited("input.cc", "someone@example.edu"))
    assert "input.cc" in message


def test_bad_due_time_names_the_field() -> None:
    message = rejected(edited("labels.items.0.due_at", "2026-11-14 07:59"))
    assert "labels.items.0.due_at" in message
    assert "UTC" in message


def test_offset_time_is_rejected() -> None:
    message = rejected(edited("input.sent_at", "2026-11-02T09:00:00-08:00"))
    assert "input.sent_at" in message


def test_unknown_header_is_rejected() -> None:
    message = rejected(edited("input.headers", {"Received": "from mail.example.com"}))
    assert "input.headers" in message


def test_bulk_headers_are_kept() -> None:
    headers = {"List-Unsubscribe": "<https://example.com/u>", "Precedence": "bulk"}
    [loaded] = load_lines(lines(edited("input.headers", headers)))
    assert loaded.input.headers == headers


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("due_ambiguous", True),
        ("due_at", None),
    ],
)
def test_due_fields_must_agree(field: str, value: Any) -> None:
    message = rejected(edited(f"labels.items.0.{field}", value))
    assert "labels.items.0" in message


def test_ambiguous_due_needs_null_due_at() -> None:
    data = edited("labels.items.0.due_at", None)
    data["labels"]["items"][0]["due_ambiguous"] = True
    [loaded] = load_lines(lines(data))
    assert loaded.labels.items[0].due_ambiguous


def test_due_raw_must_appear_in_email() -> None:
    message = rejected(edited("labels.items.0.due_raw", "Monday Nov 16 at 9am PT"))
    assert "hw9: due_raw" in message


def test_strict_booleans() -> None:
    message = rejected(edited("labels.safe_to_auto_read", "false"))
    assert "labels.safe_to_auto_read" in message


def update(changes: str, sent_at: str) -> dict[str, Any]:
    data = copy.deepcopy(sample(id="t-002"))
    data["input"]["sent_at"] = sent_at
    item = data["labels"]["items"][0]
    item["ref"] = "hw9-v2"
    item["changes"] = changes
    return data


def test_change_to_earlier_item_loads() -> None:
    later = update("hw9", "2026-11-05T17:00:00Z")
    loaded = load_lines(lines(sample(), later))
    assert loaded[1].labels.items[0].changes == "hw9"


def test_change_to_unknown_ref_is_rejected() -> None:
    later = update("hw1", "2026-11-05T17:00:00Z")
    message = rejected(sample(), later)
    assert "line 2" in message
    assert "hw9-v2.changes: unknown ref" in message


def test_change_to_later_email_is_rejected() -> None:
    earlier = update("hw9", "2026-11-01T17:00:00Z")
    message = rejected(sample(), earlier)
    assert "must be an earlier email" in message


def test_duplicate_id_is_rejected() -> None:
    other = edited("labels.items", [])
    message = rejected(sample(), other)
    assert "line 2: id: duplicate" in message


def test_duplicate_ref_is_rejected() -> None:
    other = sample(id="t-002")
    message = rejected(sample(), other)
    assert "duplicate ref" in message


def test_bad_json_line_is_rejected() -> None:
    with pytest.raises(SetError, match="line 1"):
        load_lines(['{"id": "t-001",'])
