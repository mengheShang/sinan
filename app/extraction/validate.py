"""Check the model's reply and turn it into items. Pure functions only.

Nothing the model says is trusted on its own (ADR 0007):

- The reply must match the schema exactly. Anything else is refused whole.
- `evidence` must appear in the email body, ignoring only differences in
  whitespace and Unicode form, and be at least 8 characters long. An item
  without such evidence is dropped.
- `due_raw` must appear inside the evidence. If it does not, the item is kept
  without a time and marked `needs_review`.
- `due_at` always comes from `parse_due`, never from the model.
- An "update" is kept as a new item marked `needs_review` until M2 matches it
  to the item it changes.
"""

import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from app.extraction.deadlines import parse_due
from app.extraction.schema import ExtractedItem, Extraction

# Shorter evidence, such as "HW", would match almost any email
MIN_EVIDENCE = 8


@dataclass(frozen=True)
class ValidItem:
    kind: str
    course: str | None
    title: str
    due_raw: str | None
    due_at: datetime | None
    status: str
    # Why it needs review, as a short code; None when the status is "open"
    review: str | None
    evidence: str
    change_type: str


@dataclass(frozen=True)
class Checked:
    # "ok" when the reply was valid, even with no items; "invalid" otherwise
    status: str
    items: list[ValidItem]
    # Short codes only, such as "bad_json" or "dropped=2"
    detail: str | None


def squash(text: str) -> str:
    """Unicode NFKC with every run of whitespace turned into one space."""
    normal = unicodedata.normalize("NFKC", text)
    return " ".join(normal.split())


def blank_to_none(text: str | None) -> str | None:
    if text is None:
        return None
    stripped = text.strip()
    return stripped or None


def has_evidence(item: ExtractedItem, haystack: str) -> bool:
    """Long enough to mean something, and found in the body."""
    needle = squash(item.evidence)
    if len(needle) < MIN_EVIDENCE:
        return False
    return needle in haystack


def parse_reply(raw: str) -> Extraction | str:
    try:
        data = json.loads(raw)
    except ValueError:
        return "bad_json"
    try:
        return Extraction.model_validate(data)
    except ValidationError:
        return "bad_schema"


def due_of(item: ExtractedItem, sent_at: datetime) -> tuple[datetime | None, str | None]:
    """The parsed due time, or why there is none. (None, None) means no deadline given."""
    if item.due_raw is None or not item.due_raw.strip():
        if item.kind == "deadline":
            return None, "no_due_raw"
        return None, None
    raw = squash(item.due_raw).casefold()
    evidence = squash(item.evidence).casefold()
    if raw not in evidence:
        return None, "due_not_in_evidence"
    parsed = parse_due(item.due_raw, sent_at)
    return parsed.due_at, parsed.reason


def build(item: ExtractedItem, sent_at: datetime) -> ValidItem:
    due_at, review = due_of(item, sent_at)
    if review is None and item.change_type == "update":
        review = "unmatched_update"
    status = "open" if review is None else "needs_review"
    due_raw = blank_to_none(item.due_raw)
    return ValidItem(
        kind=item.kind,
        course=blank_to_none(item.course),
        title=item.title.strip(),
        due_raw=due_raw,
        due_at=due_at,
        status=status,
        review=review,
        evidence=item.evidence,
        change_type=item.change_type,
    )


def check(raw: str, body: str, sent_at: datetime) -> Checked:
    """Validate one reply against the email body it came from."""
    reply = parse_reply(raw)
    if isinstance(reply, str):
        return Checked("invalid", [], reply)
    haystack = squash(body)
    kept: list[ValidItem] = []
    for item in reply.items:
        if not has_evidence(item, haystack) or not item.title.strip():
            continue
        kept.append(build(item, sent_at))
    dropped = len(reply.items) - len(kept)
    detail = f"dropped={dropped}" if dropped else None
    return Checked("ok", kept, detail)
