"""Check the model's reply and turn it into items. Pure functions only.

Nothing the model says is trusted on its own (ADR 0007):

- The reply must match the schema exactly. Anything else is refused whole.
- `evidence` must appear in the email body, ignoring only differences in
  whitespace and Unicode form, and be at least 8 characters long. An item
  without such evidence is dropped.
- `due_raw` must appear inside the evidence. If it does not, the item is kept
  without a time and marked `needs_review`.
- `due_at` always comes from `parse_due`, never from the model. A missing
  zone or time is assumed there (ADR 0016) and the item records which.
  No zone is assumed when the email body names any zone, and no time when
  the evidence gives a clock time that `due_raw` left out: the item is
  marked `needs_review` instead.
- An "update" is marked `needs_review` (`unmatched_update`) here. Extraction
  then matches it to the item it changes (app/items/changes.py): one match
  becomes a proposal instead of an item, and only an unmatched or ambiguous
  change is saved as an item.
- A "notice" (ADR 0017) keeps its parsed due time but never needs review:
  a due time that does not parse is left out, and the item stays `open`
  and shows its `due_raw` as written. A `due_raw` that is not inside the
  evidence is dropped instead, so a notice never shows a date the email
  does not tie to it. A notice is never a change, so it is never matched
  to another item. Choosing "notice" only makes an item less
  prominent; it gives the model no other power.
"""

import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, tzinfo

from pydantic import ValidationError

from app.extraction.deadlines import HOME, DueParse, names_time, names_zone, parse_due
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
    # Which parts of `due_at` were assumed, not written (ADR 0016)
    zone_assumed: bool = False
    time_assumed: bool = False
    # The date is the email's date plus a count ("in 3 days")
    date_computed: bool = False
    # The time of day comes from the item a change updates (ADR 0016,
    # decision 7); set only on proposals, never here
    time_carried: bool = False


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


def nothing_left_out(parsed: DueParse, evidence: str, body: str) -> DueParse:
    """Refuse an assumed zone the email states, or an assumed time the
    evidence states (ADR 0016, decision 8).

    The model may copy "Nov 18 at 10am" from "Nov 18 at 10am ET", or quote
    an injected "the real due date is Dec 31" next to a real deadline in PT.
    Assuming the owner's zone would then give a wrong time that looks right.
    """
    if parsed.zone_assumed and names_zone(body):
        return DueParse(None, "zone_outside_due")
    if parsed.time_assumed and names_time(evidence):
        return DueParse(None, "time_outside_due")
    return parsed


def due_of(item: ExtractedItem, sent_at: datetime, home: tzinfo, body: str) -> DueParse:
    """The parsed due time, or why there is none. Neither means no deadline given."""
    if item.due_raw is None or not item.due_raw.strip():
        if item.kind == "deadline":
            return DueParse(None, "no_due_raw")
        return DueParse(None, None)
    raw = squash(item.due_raw).casefold()
    evidence = squash(item.evidence).casefold()
    if raw not in evidence:
        return DueParse(None, "due_not_in_evidence")
    parsed = parse_due(item.due_raw, sent_at, home)
    return nothing_left_out(parsed, item.evidence, body)


def review_of(item: ExtractedItem, due: DueParse) -> str | None:
    """Why the item needs review, or None. A notice never needs review."""
    if item.kind == "notice":
        return None
    if due.reason is not None:
        return due.reason
    if item.change_type == "update":
        return "unmatched_update"
    return None


def is_change(item: ValidItem) -> bool:
    """An update with a parsed time; any other update stays a review item.

    A notice is never a change, so it never becomes a proposal.
    """
    if item.kind == "notice":
        return False
    return item.change_type == "update" and item.due_at is not None


def build(item: ExtractedItem, sent_at: datetime, home: tzinfo, body: str) -> ValidItem:
    due = due_of(item, sent_at, home, body)
    review = review_of(item, due)
    status = "open" if review is None else "needs_review"
    due_raw = blank_to_none(item.due_raw)
    if item.kind == "notice" and due.reason == "due_not_in_evidence":
        due_raw = None
    return ValidItem(
        kind=item.kind,
        course=blank_to_none(item.course),
        title=item.title.strip(),
        due_raw=due_raw,
        due_at=due.due_at,
        status=status,
        review=review,
        evidence=item.evidence,
        change_type=item.change_type,
        zone_assumed=due.zone_assumed,
        time_assumed=due.time_assumed,
        date_computed=due.date_computed,
    )


def check(raw: str, body: str, sent_at: datetime, home: tzinfo = HOME) -> Checked:
    """Validate one reply against the email body it came from.

    `home` is the owner's zone (SINAN_TIMEZONE), assumed when a due time
    names none.
    """
    reply = parse_reply(raw)
    if isinstance(reply, str):
        return Checked("invalid", [], reply)
    haystack = squash(body)
    kept: list[ValidItem] = []
    for item in reply.items:
        if not has_evidence(item, haystack) or not item.title.strip():
            continue
        kept.append(build(item, sent_at, home, body))
    dropped = len(reply.items) - len(kept)
    detail = f"dropped={dropped}" if dropped else None
    return Checked("ok", kept, detail)
