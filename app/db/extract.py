"""Run prefilter and extraction over stored messages, and save the results.

For each message that has never been extracted, under any prompt version:

1. The prefilter decides. A skip is saved as a `skipped` run with its reason
   code, and the message never reaches the model.
2. Otherwise the prepared text goes to the extractor, at most `cap` messages
   per cycle (ADR 0007).
3. The reply is validated. Valid items are saved with their first
   `item_versions` row, which points at the message, the run and the evidence.
   Its `after` data also says whether the due time's zone or time was
   assumed, or its date computed from the email date (ADR 0016):
   `zone_assumed`, `time_assumed`, `date_computed` and `time_carried`
   (the last is true only on a version an approved proposal appends).
4. An item the model marks as an update, with a parsed due time, is matched
   to the item it changes (app/items/changes.py). One match becomes a
   pending proposal instead of a new item. With several matches or none,
   the link picker, when given, asks the model once to pick from a list
   built by deterministic code (ADR 0019); a pick also becomes only a
   pending proposal. A change left without a target is saved as a new item
   marked needs_review. A notice is never matched (ADR 0017).
5. Sender trust (ADR 0010) is decided from the sender and Gmail's stored
   authentication results, never the body. A deadline or to-do from an
   untrusted sender is saved as needs_review (`untrusted_sender`), and a
   change from one is never matched or proposed. Each version's `after`
   records the trust reason code as `sender_trust`.

A message with a run under any prompt version is done, so a rerun skips it,
and a new prompt version changes only how new mail is read (ADR 0016,
"Consequences": re-extracting older mail is a separate, deliberate step).
The table still allows one run per message and prompt version, for that step.
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
from app.extraction.link_prompt import LINK_PROMPT_VERSION
from app.extraction.link_reply import Code
from app.extraction.prefilter import Envelope, Rules, decide
from app.extraction.prompt import PROMPT, PROMPT_HASH, PROMPT_VERSION, prepare
from app.extraction.trust import Trust, sender_trust, with_school
from app.extraction.validate import Checked, ValidItem, check, hold_untrusted, is_change
from app.items.changes import Link, Source, propose
from app.items.linking import FAILED, NO_CHOICES
from app.providers.llm import Extractor, ExtractorError, LinkPicker

# Link replies refused by validation (app/extraction/link_reply.py)
REFUSED: tuple[Code, ...] = ("bad_json", "bad_schema", "out_of_list")

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
    # The link step (ADR 0019): model calls, picks, refused replies, failed
    # calls, and proposals linked by the model
    link_asked: int = 0
    link_picked: int = 0
    link_refused: int = 0
    link_failed: int = 0
    llm_proposals: int = 0
    # Emails sent to the model whose sender is not trusted (ADR 0010)
    untrusted: int = 0
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
            "link_prompt_version": LINK_PROMPT_VERSION,
            "link_asked": self.link_asked,
            "link_picked": self.link_picked,
            "link_refused": self.link_refused,
            "link_failed": self.link_failed,
            "llm_proposals": self.llm_proposals,
            "untrusted": self.untrusted,
            "stopped": self.stopped,
            "skip_reasons": dict(sorted(self.reasons.items())),
        }


def pending(session: Session) -> list[tuple[uuid.UUID, str]]:
    """IDs and account roles of messages with no run under any prompt version.

    A new prompt version applies to new mail only. Treating every older
    message as pending again would send all stored mail back to the model
    and save its items a second time.
    """
    done = select(ExtractionRun.message_id)
    query = (
        select(Message.id, Account.role)
        .join(Account, Account.id == Message.account_id)
        .where(Message.id.not_in(done))
        .order_by(Message.received_at, Message.id)
    )
    rows = session.execute(query).all()
    return [(row[0], row[1]) for row in rows]


def school_addresses(session: Session) -> list[str]:
    """Addresses of the connected school accounts, whose domains are trusted."""
    query = select(Account.email).where(Account.role == "school")
    return list(session.execute(query).scalars())


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
        "time_carried": item.time_carried,
        "sender_trust": item.sender_trust,
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
    home: tzinfo = HOME
    picker: LinkPicker | None = None


def count_link(link: Link, counts: Counts) -> None:
    """Count the link step's outcome for the cycle's audit event."""
    pick = link.pick
    if pick is None or pick.code == NO_CHOICES:
        return
    counts.link_asked += 1
    if pick.code == "picked":
        counts.link_picked += 1
    elif pick.code == FAILED:
        counts.link_failed += 1
    elif pick.code in REFUSED:
        counts.link_refused += 1


def save_change(
    session: Session, item: ValidItem, source: Source, ctx: Context, counts: Counts
) -> int:
    """Propose a change, or save it as an item. Returns the number of items saved."""
    result, link = propose(session, item, source, ctx.now, ctx.ttl_days, ctx.home, ctx.picker)
    count_link(link, counts)
    if result == "proposed":
        counts.proposals += 1
        if link.path == "llm":
            counts.llm_proposals += 1
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
    # The link step (ADR 0019); None links by the deterministic matcher only
    picker: LinkPicker | None = None


def extract_one(
    session: Session,
    msg: Message,
    extractor: Extractor,
    counts: Counts,
    policy: ChangePolicy,
    trust: Trust,
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
    checked = hold_untrusted(checked, trust)
    if not trust.trusted:
        counts.untrusted += 1
    now = policy.clock()
    ctx = Context(extractor.model, ms, now, policy.ttl_days, policy.home, policy.picker)
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
    picker: LinkPicker | None = None,
) -> Counts:
    """Process pending messages, sending at most `cap` to the model. Commits each one.

    `ttl_days` is SINAN_PROPOSAL_TTL_DAYS and `clock` gives the creation time,
    both for proposals made from changes. `home` is SINAN_TIMEZONE. `picker`
    is the link step (ADR 0019); a service error from it ends the cycle like
    one from the extractor.
    """
    counts = Counts()
    policy = ChangePolicy(ttl_days, clock, home, picker)
    rules = with_school(rules, school_addresses(session))
    sent = 0
    for msg_id, role in pending(session):
        msg = session.get_one(Message, msg_id)
        env = envelope(msg, role)
        decision = decide(env, rules)
        if decision.action == "skip":
            skip(session, msg, decision.reason, counts)
            session.commit()
            continue
        if sent >= cap:
            # Skips above cost nothing, so they keep going; model calls wait
            continue
        sent += 1
        try:
            extract_one(session, msg, extractor, counts, policy, sender_trust(env, rules))
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
