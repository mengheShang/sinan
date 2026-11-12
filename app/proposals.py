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
from datetime import datetime, timedelta
from typing import Any

HASH_PATTERN = r"^[0-9a-f]{64}$"


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
