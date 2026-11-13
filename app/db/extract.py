"""Run prefilter and extraction over stored messages, and save the results.

For each message without a run for the current prompt version:

1. The prefilter decides. A skip is saved as a `skipped` run with its reason
   code, and the message never reaches the model.
2. Otherwise the prepared text goes to the extractor, at most `cap` messages
   per cycle (ADR 0007).
3. The reply is validated. Valid items are saved with their first
   `item_versions` row, which points at the message, the run and the evidence.
   Its `after` data also says whether the due time's zone or time was
   assumed, or its date computed from the email date (ADR 0016):
   `zone_assumed`, `time_assumed` and `date_computed`.
4. An item the model marks as an update, with a parsed due time, is matched
   to the item it changes (app/items/changes.py). One match becomes a
   pending proposal instead of a new item; several matches or none save the
   change as a new item marked needs_review.

One run row per message and prompt version, so a rerun skips what is done.
A service error (network, rate limit, bad key) saves nothing and ends the
cycle, so those messages are tried again next time.
"""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Account, AuditEvent, ExtractionRun, Item, ItemVersion, Message
from app.extraction.deadlines import HOME
from app.extraction.prefilter import Envelope, Rules, decide
from app.extraction.prompt import PROMPT, PROMPT_HASH, PROMPT_VERSION, prepare
from app.extraction.validate import Checked, ValidItem, check
from app.items.changes import Source, propose
from app.providers.llm import Extractor, ExtractorError

log = logging.getLogger("sinan.worker.extract")


@dataclass
class Counts:
    skipped: int = 0
    extracted: int = 0
    invalid: int = 0
    errors: int = 0
    items: int = 0
    # Deadline changes: proposed, already current, older than the item's
    # time, or with several candidates (saved as needs_review items)
    proposals: int = 0
    unchanged: int = 0
    stale: int = 0
    ambiguous: int = 0
    # Set when a service error ended the cycle early
    stopped: str | None = None
    reasons: dict[str, int] = field(default_factory=dict)

    def as_data(self) -> dict[str, object]:
        return {
            "prompt_version": PROMPT_VERSION,
            "skipped": self.skipped,
            "extracted": self.extracted,
            "invalid": self.invalid,
            "errors": self.errors,
            "items": self.items,
            "proposals": self.proposals,
            "unchanged": self.unchanged,
            "stale": self.stale,
            "ambiguous": self.ambiguous,
            "stopped": self.stopped,
            "skip_reasons": dict(sorted(self.reasons.items())),
        }


def pending(session: Session) -> list[tuple[uuid.UUID, str]]:
    """IDs and account roles of messages with no run for this prompt version."""
    done = select(ExtractionRun.message_id).where(ExtractionRun.prompt_version == PROMPT_VERSION)
    query = (
        select(Message.id, Account.role)
        .join(Account, Account.id == Message.account_id)
        .where(Message.id.not_in(done))
        .order_by(Message.received_at, Message.id)
    )
    rows = session.execute(query).all()
    return [(row[0], row[1]) for row in rows]


def envelope(msg: Message, role: str) -> Envelope:
    return Envelope(role, msg.from_addr, msg.subject, msg.headers or {})


def new_run(msg: Message, status: str, model: str | None, detail: str | None) -> ExtractionRun:
    return ExtractionRun(
        message_id=msg.id,
        prompt_version=PROMPT_VERSION,
        prompt_hash=PROMPT_HASH,
        model=model,
        status=status,
        detail=detail,
    )


def after_fields(item: ValidItem) -> dict[str, object]:
    due_at = item.due_at.isoformat() if item.due_at else None
    return {
        "kind": item.kind,
        "course": item.course,
        "title": item.title,
        "due_raw": item.due_raw,
        "due_at": due_at,
        "status": item.status,
        "review": item.review,
        "model_change_type": item.change_type,
        "zone_assumed": item.zone_assumed,
        "time_assumed": item.time_assumed,
        "date_computed": item.date_computed,
    }


def save_item(session: Session, item: ValidItem, msg: Message, run: ExtractionRun) -> None:
    row = Item(
        kind=item.kind,
        course=item.course,
        title=item.title,
        due_raw=item.due_raw,
        due_at=item.due_at,
        status=item.status,
    )
    session.add(row)
    session.flush()
    version = ItemVersion(
        item_id=row.id,
        version=1,
        change_type="create",
        actor="extraction",
        before=None,
        after=after_fields(item),
        evidence=item.evidence,
        message_id=msg.id,
        extraction_run_id=run.id,
    )
    session.add(version)


@dataclass(frozen=True)
class Context:
    """What saving a reply needs besides the reply itself."""

    model: str
    ms: int
    now: datetime
    ttl_days: int


def is_change(item: ValidItem) -> bool:
    """An update with a parsed time; any other update stays a review item."""
    return item.change_type == "update" and item.due_at is not None


def save_change(
    session: Session, item: ValidItem, source: Source, ctx: Context, counts: Counts
) -> int:
    """Propose a change, or save it as an item. Returns the number of items saved."""
    result = propose(session, item, source, ctx.now, ctx.ttl_days)
    if result == "proposed":
        counts.proposals += 1
        return 0
    if result == "unchanged":
        counts.unchanged += 1
        return 0
    if result == "stale":
        counts.stale += 1
        return 0
    if result == "several":
        counts.ambiguous += 1
        item = replace(item, status="needs_review", review="ambiguous_update")
    save_item(session, item, source.message, source.run)
    return 1


def save_checked(
    session: Session, msg: Message, checked: Checked, ctx: Context, counts: Counts
) -> int:
    run = new_run(msg, checked.status, ctx.model, checked.detail)
    run.item_count = len(checked.items)
    run.duration_ms = ctx.ms
    session.add(run)
    session.flush()
    source = Source(msg, run, sent_time(msg))
    saved = 0
    for item in checked.items:
        if is_change(item):
            saved += save_change(session, item, source, ctx, counts)
            continue
        save_item(session, item, msg, run)
        saved += 1
    return saved


def sent_time(msg: Message) -> datetime:
    """The Date header when present, else when Gmail received it."""
    return msg.sent_at or msg.received_at


def utc_now() -> datetime:
    return datetime.now(UTC)


Clock = Callable[[], datetime]


@dataclass(frozen=True)
class ChangePolicy:
    """How proposals made from deadline changes are timed, and the owner's
    zone, assumed when a due time names none."""

    ttl_days: int
    clock: Clock
    home: tzinfo = HOME


def extract_one(
    session: Session,
    msg: Message,
    extractor: Extractor,
    counts: Counts,
    policy: ChangePolicy,
) -> None:
    """Send one message to the model and save the outcome. May raise a service error."""
    when = sent_time(msg)
    text = prepare(msg.subject, msg.from_addr, when, msg.body_text or "")
    start = time.monotonic()
    try:
        raw = extractor.extract(PROMPT, text)
    except ExtractorError as err:
        if err.scope == "service":
            raise
        session.add(new_run(msg, "error", extractor.model, err.code))
        counts.errors += 1
        return
    ms = int((time.monotonic() - start) * 1000)
    checked = check(raw, msg.body_text or "", when, policy.home)
    ctx = Context(extractor.model, ms, policy.clock(), policy.ttl_days)
    counts.items += save_checked(session, msg, checked, ctx, counts)
    if checked.status == "ok":
        counts.extracted += 1
    else:
        counts.invalid += 1


def skip(session: Session, msg: Message, reason: str, counts: Counts) -> None:
    session.add(new_run(msg, "skipped", None, reason))
    counts.skipped += 1
    counts.reasons[reason] = counts.reasons.get(reason, 0) + 1


def extract_pending(
    session: Session,
    extractor: Extractor,
    rules: Rules,
    cap: int,
    ttl_days: int = 7,
    clock: Clock = utc_now,
    home: tzinfo = HOME,
) -> Counts:
    """Process pending messages, sending at most `cap` to the model. Commits each one.

    `ttl_days` is SINAN_PROPOSAL_TTL_DAYS and `clock` gives the creation time,
    both for proposals made from changes. `home` is SINAN_TIMEZONE.
    """
    counts = Counts()
    policy = ChangePolicy(ttl_days, clock, home)
    sent = 0
    for msg_id, role in pending(session):
        msg = session.get_one(Message, msg_id)
        decision = decide(envelope(msg, role), rules)
        if decision.action == "skip":
            skip(session, msg, decision.reason, counts)
            session.commit()
            continue
        if sent >= cap:
            # Skips above cost nothing, so they keep going; model calls wait
            continue
        sent += 1
        try:
            extract_one(session, msg, extractor, counts, policy)
        except ExtractorError as err:
            session.rollback()
            counts.stopped = err.code
            log.warning("Extraction stopped: %s", err.code)
            break
        session.commit()
    audit(session, counts)
    session.commit()
    return counts


def audit(session: Session, counts: Counts) -> None:
    event = AuditEvent(event_type="extraction", actor="worker", data=counts.as_data())
    session.add(event)
