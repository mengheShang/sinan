"""Payload hash and expiry for proposals. Pure functions, no database."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.proposals import PayloadError, canonical, expiry, payload_hash

PAYLOAD = {
    "due_at": "2026-11-17T07:59:00Z",
    "title": "Homework 3",
    "meta": {"base_version": 2, "fields": ["due_at", "title"]},
}
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def test_hash_is_sha256_hex() -> None:
    digest = payload_hash(PAYLOAD)
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_key_order_does_not_matter() -> None:
    shuffled = {
        "meta": {"fields": ["due_at", "title"], "base_version": 2},
        "title": "Homework 3",
        "due_at": "2026-11-17T07:59:00Z",
    }
    assert payload_hash(shuffled) == payload_hash(PAYLOAD)


def test_canonical_form_is_fixed() -> None:
    text = canonical({"b": [1, True, None], "a": "Café"})
    assert text == '{"a":"Café","b":[1,true,null]}'.encode()


@pytest.mark.parametrize(
    "changed",
    [
        {**PAYLOAD, "due_at": "2026-11-17T07:59:01Z"},
        {**PAYLOAD, "title": "Homework 3 "},
        {**PAYLOAD, "meta": {"base_version": 3, "fields": ["due_at", "title"]}},
        {**PAYLOAD, "meta": {"base_version": 2, "fields": ["title", "due_at"]}},
        {**PAYLOAD, "meta": {"base_version": "2", "fields": ["due_at", "title"]}},
        {**PAYLOAD, "extra": None},
        {"due_at": PAYLOAD["due_at"], "title": PAYLOAD["title"]},
    ],
)
def test_any_change_changes_the_hash(changed: dict[str, object]) -> None:
    assert payload_hash(changed) != payload_hash(PAYLOAD)


def test_true_and_one_hash_differently() -> None:
    assert payload_hash({"a": True}) != payload_hash({"a": 1})


@pytest.mark.parametrize(
    "bad",
    [
        {"due_at": NOW},
        {"score": 1.0},
        {"nested": {"value": float("nan")}},
        {"items": [{1, 2}]},
        {"inner": {1: "x"}},
    ],
)
def test_values_without_one_form_are_refused(bad: dict[str, object]) -> None:
    with pytest.raises(PayloadError):
        payload_hash(bad)


def test_expiry_is_the_ttl_without_a_due_time() -> None:
    assert expiry(NOW, None, 7) == NOW + timedelta(days=7)


def test_expiry_is_the_due_time_when_sooner() -> None:
    due = NOW + timedelta(days=2)
    assert expiry(NOW, due, 7) == due


def test_expiry_is_the_ttl_when_due_is_later() -> None:
    due = NOW + timedelta(days=30)
    assert expiry(NOW, due, 7) == NOW + timedelta(days=7)


def test_expiry_needs_aware_times() -> None:
    naive = NOW.replace(tzinfo=None)
    with pytest.raises(ValueError):
        expiry(naive, None, 7)
    with pytest.raises(ValueError):
        expiry(NOW, naive, 7)
    with pytest.raises(ValueError):
        expiry(NOW, None, 0)


def test_ttl_setting_defaults_to_seven_days() -> None:
    assert Settings().sinan_proposal_ttl_days == 7


@pytest.mark.parametrize("days", [1, 30])
def test_ttl_setting_accepts_its_bounds(days: int) -> None:
    assert Settings(sinan_proposal_ttl_days=days).sinan_proposal_ttl_days == days


@pytest.mark.parametrize("days", [0, 31, 12345])
def test_ttl_setting_outside_range_is_refused(days: int) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(sinan_proposal_ttl_days=days)
    message = str(caught.value)
    assert "SINAN_PROPOSAL_TTL_DAYS must be 1 to 30 days" in message
    assert "input_value" not in message
    if days > 100:
        assert str(days) not in message


def test_ttl_setting_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SINAN_PROPOSAL_TTL_DAYS", "31")
    with pytest.raises(ValidationError, match="SINAN_PROPOSAL_TTL_DAYS"):
        Settings()
    monkeypatch.setenv("SINAN_PROPOSAL_TTL_DAYS", "30")
    assert Settings().sinan_proposal_ttl_days == 30
