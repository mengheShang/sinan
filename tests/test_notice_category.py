"""Notice categories (ADR 0020): validation, storage defaults and grouping.

Pure tests on synthetic replies; the database and API side is in
tests/test_notices.py. Every course, person and address is fictional.
"""

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from app.extraction.prompt import PROMPT
from app.extraction.schema import OUTPUT_SCHEMA
from app.extraction.validate import check
from app.items.category import CATEGORIES, OTHER, category_of, group, stored_category

SENT = datetime(2026, 11, 10, 17, 0, tzinfo=UTC)
ALERT = "A fictional safety advisory: avoid the north parking lot tonight."
ESSAY = "Essay 2 is due Friday Nov 20 at 11:59pm PT."


def entry(kind: str, evidence: str, **changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": kind,
        "course": None,
        "title": "Safety advisory: north lot",
        "due_raw": None,
        "evidence": evidence,
        "change_type": "create",
    }
    base.update(changes)
    return base


def checked_category(raw_item: dict[str, Any], body: str = ALERT) -> str | None:
    reply = json.dumps({"items": [raw_item]})
    result = check(reply, body, SENT)
    assert result.status == "ok"
    (only,) = result.items
    return only.category


def test_a_known_category_is_kept() -> None:
    assert checked_category(entry("notice", ALERT, category="safety")) == "safety"


@pytest.mark.parametrize("value", ["urgent", "Safety", "", "deadline", None])
def test_an_unknown_or_null_category_becomes_other(value: str | None) -> None:
    assert checked_category(entry("notice", ALERT, category=value)) == OTHER


def test_a_reply_without_a_category_reads_as_other() -> None:
    # Replies recorded before prompt v6 have no category field
    assert checked_category(entry("notice", ALERT)) == OTHER


def test_a_bad_category_does_not_refuse_the_reply() -> None:
    raw = entry("notice", ALERT, category="x" * 50)
    assert checked_category(raw) == OTHER
    too_long = json.dumps({"items": [entry("notice", ALERT, category="x" * 51)]})
    assert check(too_long, ALERT, SENT).detail == "bad_schema"


def test_a_required_item_gets_no_category() -> None:
    raw = entry("deadline", ESSAY, title="Essay 2", due_raw="Friday Nov 20 at 11:59pm PT")
    raw["category"] = "course"
    assert checked_category(raw, ESSAY) is None


def test_old_data_without_a_category_reads_as_other() -> None:
    assert stored_category(None) == OTHER
    assert stored_category({"kind": "notice", "title": "Flu shots"}) == OTHER
    assert stored_category({"category": None}) == OTHER
    assert stored_category({"category": "rumour"}) == OTHER
    assert stored_category({"category": "health"}) == "health"


def test_grouping_gives_one_row_per_category_in_fixed_order() -> None:
    notices = [
        ("Storm warning", "safety"),
        ("CS 000 forum digest", "course"),
        ("Package delivered", "orders"),
        ("Crime alert", "safety"),
        ("Mystery", "nonsense"),
        ("Fall concert", None),
    ]
    groups = group(notices, lambda n: category_of(n[1]))
    names = [name for name, _ in groups]
    assert names == ["course", "safety", "orders", "other"]
    members = {name: [title for title, _ in found] for name, found in groups}
    assert members["safety"] == ["Storm warning", "Crime alert"]
    assert members["other"] == ["Mystery", "Fall concert"]


def test_grouping_nothing_gives_no_rows() -> None:
    assert group([], lambda n: n) == []


def test_schema_and_prompt_list_the_same_categories() -> None:
    props = OUTPUT_SCHEMA["properties"]["items"]["items"]["properties"]
    allowed, null = props["category"]["anyOf"]
    assert allowed["enum"] == list(CATEGORIES)
    assert null == {"type": "null"}
    for name in CATEGORIES:
        assert f'"{name}"' in PROMPT
