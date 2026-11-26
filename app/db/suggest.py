"""Ask the model for suggested due times, and store them (ADR 0022, decision 4).

For each active deadline or to-do with no due time and no suggestion yet,
newest first and at most `cap` per cycle, the model is asked once with the
email that created the item. The reply is validated by
app.extraction.suggest and stored in `due_suggestions`, with a reason when
there is no suggestion, so the item is never asked about again.

Nothing here changes an item. A suggestion only prefills the owner's due
form; the owner's save, through the API, is the only thing that sets a due
time (Issue #113).

A call that fails for this item is stored as "error". A service failure
(network, rate limit, bad key) stops the step and stores nothing for that
item, so it is asked again next cycle. The cycle's audit event holds counts
only, never text.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, tzinfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditEvent, DueSuggestion, Item, ItemVersion, Message
from app.extraction.prompt import prepare
from app.extraction.suggest import (
    SUGGEST_PROMPT,
    SUGGEST_PROMPT_HASH,
    SUGGEST_PROMPT_VERSION,
    Suggestion,
    check,
    no_suggestion,
    prepare_suggest,
)
from app.providers.llm import ExtractorError, Suggester

ACTIVE = ("open", "needs_review")
DUE_KINDS = ("deadline", "todo")


@dataclass
class SuggestCounts:
    asked: int = 0
    suggested: int = 0
    # The model said the email gives no due time
    none: int = 0
    # Refused by validation
    refused: int = 0
    errors: int = 0
    # Set when a service error ended the step early
    stopped: str | None = None


def waiting(session: Session, cap: int) -> list[tuple[Item, Message]]:
    """Items to ask about, newest first, with the email that created them."""
    asked = select(DueSuggestion.item_id)
    query = (
        select(Item, Message)
        .join(ItemVersion, (ItemVersion.item_id == Item.id) & (ItemVersion.version == 1))
        .join(Message, Message.id == ItemVersion.message_id)
        .where(
            Item.status.in_(ACTIVE),
            Item.kind.in_(DUE_KINDS),
            Item.due_at.is_(None),
            Item.id.not_in(asked),
        )
        .order_by(Item.created_at.desc(), Item.id)
        .limit(cap)
    )
    rows = session.execute(query).all()
    return [(row[0], row[1]) for row in rows]


def sent_time(msg: Message) -> datetime:
    return msg.sent_at or msg.received_at


def ask(suggester: Suggester, item: Item, msg: Message, zone: tzinfo) -> Suggestion:
    """One model call and its check. Raises ExtractorError of scope "service"."""
    when = sent_time(msg)
    body = msg.body_text or ""
    email = prepare(msg.subject, msg.from_addr, when, body)
    text = prepare_suggest(email, item.title, item.course)
    try:
        raw = suggester.suggest(SUGGEST_PROMPT, text)
    except ExtractorError as err:
        if err.scope == "service":
            raise
        return no_suggestion("error")
    shown = f"{msg.subject or ''}\n{body}"
    return check(raw, shown, when, zone)


def row_for(item: Item, msg: Message, found: Suggestion, model: str) -> DueSuggestion:
    return DueSuggestion(
        item_id=item.id,
        message_id=msg.id,
        due_at=found.due_at,
        zone_assumed=found.due_at is not None,
        time_assumed=found.time_assumed,
        quote=found.quote,
        reason=found.reason,
        model=model,
        prompt_version=SUGGEST_PROMPT_VERSION,
        prompt_hash=SUGGEST_PROMPT_HASH,
    )


def count(found: Suggestion, counts: SuggestCounts) -> None:
    counts.asked += 1
    if found.due_at is not None:
        counts.suggested += 1
    elif found.reason == "none":
        counts.none += 1
    elif found.reason == "error":
        counts.errors += 1
    else:
        counts.refused += 1


def suggest_pending(
    session: Session, suggester: Suggester, cap: int, zone: tzinfo
) -> SuggestCounts:
    """Ask about up to `cap` items. Commits after each item and at the end."""
    counts = SuggestCounts()
    for item, msg in waiting(session, cap):
        try:
            found = ask(suggester, item, msg, zone)
        except ExtractorError as err:
            counts.stopped = err.code
            break
        session.add(row_for(item, msg, found, suggester.model))
        session.commit()
        count(found, counts)
    if counts.asked or counts.stopped:
        data = {**asdict(counts), "prompt_version": SUGGEST_PROMPT_VERSION}
        session.add(AuditEvent(event_type="suggest.cycle", actor="worker", data=data))
        session.commit()
    return counts
