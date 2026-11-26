"""Proposal payloads: canonical form, hash and expiry. Pure functions only.

A proposal's `payload_hash` is the SHA-256 of its canonical payload. It is
computed when the proposal is made and again before it is applied; a
mismatch is refused (ADR 0009, decision 2). The canonical form must therefore
never change for a given payload:

- keys are sorted, at every depth;
- no whitespace between tokens;
- non-ASCII text is kept as UTF-8, not escaped;
- only strings, integers, booleans, null, lists and objects with string keys
  are allowed. Floats are refused because 1 and 1.0 would hash differently,
  and times must already be text (UTC, ending in Z), so the caller decides
  their exact form.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from app.items.assumed import NOTHING, Assumed, assumed_fields

HASH_PATTERN = r"^[0-9a-f]{64}$"
# The only kind that changes an item; the M3 kinds are external actions
ITEM_UPDATE = "item_update"

# How an item_update proposal's item was chosen (ADR 0019)
Link = Literal["deterministic", "llm"]
DETERMINISTIC: Link = "deterministic"
LLM: Link = "llm"


class PayloadError(ValueError):
    """The payload holds a value that has no single canonical form."""


def check_value(value: Any, path: str) -> None:
    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            check_value(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for name, item in value.items():
            if not isinstance(name, str):
                raise PayloadError(f"{path}: keys must be strings")
            check_value(item, f"{path}.{name}")
        return
    kind = type(value).__name__
    raise PayloadError(f"{path}: {kind} is not allowed in a payload")


def canonical(payload: dict[str, Any]) -> bytes:
    """The payload as canonical JSON bytes."""
    check_value(payload, "payload")
    text = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return text.encode("utf-8")


def payload_hash(payload: dict[str, Any]) -> str:
    """SHA-256 of the canonical payload, as 64 lowercase hex digits."""
    return hashlib.sha256(canonical(payload)).hexdigest()


def expiry(created: datetime, due: datetime | None, ttl_days: int) -> datetime:
    """When a proposal expires: after the TTL, or at the due time if sooner.

    ADR 0009, decision 6. Both times must be timezone-aware.
    """
    if created.tzinfo is None:
        raise ValueError("created must be timezone-aware")
    if ttl_days < 1:
        raise ValueError("ttl_days must be at least 1")
    limit = created + timedelta(days=ttl_days)
    if due is None:
        return limit
    if due.tzinfo is None:
        raise ValueError("due must be timezone-aware")
    return min(limit, due)


# ---- Validity (ADR 0009, decision 6) ----

PENDING = "pending"
VALID = "valid"
EXPIRED = "expired"
SUPERSEDED = "superseded"


def validity(
    status: str,
    base_version: int,
    current_version: int,
    expires_at: datetime,
    now: datetime,
) -> str:
    """Whether a proposal can still be decided: "valid", "expired",
    "superseded", or its own status when it is no longer pending.

    The one check used both when proposals are read and right before one is
    applied. Superseded wins over expired: when the item has already changed,
    the stale proposal needs no review of its own.
    """
    if status != PENDING:
        return status
    if current_version != base_version:
        return SUPERSEDED
    if now >= expires_at:
        return EXPIRED
    return VALID


# ---- item_update payloads ----


def utc_text(when: datetime | None) -> str | None:
    """A UTC time as canonical text, such as 2026-11-17T07:59:00Z."""
    if when is None:
        return None
    if when.tzinfo is None:
        raise ValueError("time must be timezone-aware")
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: str | None) -> datetime | None:
    """The inverse of utc_text."""
    if text is None:
        return None
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def item_update(
    item_id: str,
    before: tuple[str | None, datetime | None],
    after: tuple[str | None, datetime | None],
    evidence: str,
    before_assumed: Assumed = NOTHING,
    after_assumed: Assumed = NOTHING,
    link: Link = DETERMINISTIC,
    new_item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The payload of an item_update proposal: a new due time for one item.

    `before` and `after` are (due_raw, due_at), each with the parts that
    were assumed (ADR 0016). `link` says how the item was chosen (ADR
    0019). The hash covers what the owner sees: both times, their assumed
    parts, the evidence and the link.

    `new_item` is set when the change came from an item the model called
    new (ADR 0023, decision 1): the item as extraction would have saved it.
    Rejecting the proposal saves it (decision 5). The hash covers it too, so
    the item saved is exactly the one proposed. Without it the key is left
    out, so earlier payloads keep their hash.
    """
    old = {"due_raw": before[0], "due_at": utc_text(before[1])}
    new = {"due_raw": after[0], "due_at": utc_text(after[1])}
    payload: dict[str, Any] = {
        "item_id": item_id,
        "before": {**old, **assumed_fields(before_assumed)},
        "after": {**new, **assumed_fields(after_assumed)},
        "evidence": evidence,
        "link": link,
    }
    if new_item is not None:
        payload["new_item"] = new_item
    return payload


def new_item_of(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The item a create-made proposal would save when rejected, or None."""
    found = payload.get("new_item")
    return found if isinstance(found, dict) else None


def link_of(payload: dict[str, Any]) -> Link:
    """How the payload's item was chosen. A payload from before ADR 0019 has
    no `link` and reads as deterministic, as do unknown values."""
    if payload.get("link") == LLM:
        return LLM
    return DETERMINISTIC
