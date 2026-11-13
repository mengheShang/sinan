"""Draw the real development and test sets from synced mail (Issue #41).

The mix the owner approved (2026-11-12): dev 100 emails and test 50; in each
set, half from the school mailbox and half from the personal one; 20% of
each half from mail the prefilter skips, so recall on skipped mail can be
measured too.

How a draw works:

1. Every message that can be frozen as a label snapshot is a candidate.
   Messages from an earlier draw are left out, so draws never overlap.
2. Each candidate falls in one stratum: its account role, and whether the
   current prefilter rules skip its frozen snapshot.
3. Each stratum's candidates are sorted by id and shuffled with a generator
   seeded from the seed, the set version and the stratum. The test set takes
   the first ones, the dev set the next ones. The same seed over the same
   mail gives the same draw.
4. If any stratum has fewer candidates than the sets need, nothing is
   drawn and every shortfall is reported. No stratum tops up another.

A draw is recorded as audit events, which are append-only: one
`eval_sample.draw` event with counts, and one `eval_sample.pick` event per
email naming its set and set version. So the draw exists before any label,
and cannot be edited or removed afterwards.

The test set is drawn once, with set version 1, and never again. The dev
set can be redrawn: the new draw takes the next set version and only mail
outside every earlier draw. Labels of an earlier dev draw keep their version.

Events and results hold ids, roles, strata and counts only, never content.
"""

import random
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import Integer, and_, false, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Subquery

from app.db import labels as store
from app.db.label_format import Snapshot
from app.db.models import AuditEvent, EvalLabel, Message
from app.extraction.prefilter import Envelope, Rules, decide

DRAW_EVENT = "eval_sample.draw"
PICK_EVENT = "eval_sample.pick"

ROLES = ("school", "personal")
STRATA = ("passed", "skipped")
# Test first, so a dev redraw can never take mail the test set needs
SET_ORDER = ("test", "dev")
SIZES = {"dev": 100, "test": 50}
SKIPPED_SHARE = 0.2

# Any fixed number: serializes draws, so two cannot interleave
LOCK_KEY = 41_0041

Stratum = tuple[str, str]


class DrawError(Exception):
    """The draw cannot run. The message holds counts and names only."""


# ---- Quotas ----


def quotas(size: int) -> dict[Stratum, int]:
    """How many emails a set of `size` takes from each (role, stratum)."""
    per_role = size // len(ROLES)
    skipped = round(per_role * SKIPPED_SHARE)
    found = {}
    for role in ROLES:
        found[(role, "skipped")] = skipped
        found[(role, "passed")] = per_role - skipped
    return found


# ---- Candidates ----


@dataclass(frozen=True)
class Candidate:
    id: uuid.UUID
    role: str
    stratum: str


@dataclass
class Pool:
    candidates: list[Candidate] = field(default_factory=list)
    # Messages that cannot be frozen, so could never be labeled
    unlabelable: int = 0
    # Messages already in an earlier draw
    drawn_before: int = 0


def stratum_of(snapshot: Snapshot, rules: Rules) -> str:
    """Prefilter outcome on exactly what the evaluation will see."""
    env = Envelope(snapshot.role, snapshot.from_addr, snapshot.subject, snapshot.headers)
    decision = decide(env, rules)
    return "skipped" if decision.action == "skip" else "passed"


def pool(session: Session, rules: Rules, exclude: set[uuid.UUID]) -> Pool:
    found = Pool()
    ids = session.execute(select(Message.id).order_by(Message.id)).scalars()
    for message_id in ids:
        if message_id in exclude:
            found.drawn_before += 1
            continue
        try:
            snapshot = store.freeze(session, message_id)
        except ValidationError:
            found.unlabelable += 1
            continue
        stratum = stratum_of(snapshot, rules)
        found.candidates.append(Candidate(message_id, snapshot.role, stratum))
    return found


# ---- The draw (pure) ----


@dataclass(frozen=True)
class Pick:
    message_id: uuid.UUID
    eval_set: str
    role: str
    stratum: str


@dataclass(frozen=True)
class Shortfall:
    role: str
    stratum: str
    needed: int
    available: int


class Short(DrawError):
    def __init__(self, found: list[Shortfall]) -> None:
        super().__init__(f"{len(found)} strata are short")
        self.found = found


def shuffled(ids: list[uuid.UUID], seed: int, version: int, key: Stratum) -> list[uuid.UUID]:
    # A string seed is hashed with SHA-512, so it is stable across runs
    # Repeatable on purpose: a sample, not a secret
    rng = random.Random(f"sinan-sample:{seed}:{version}:{key[0]}:{key[1]}")  # noqa: S311
    order = sorted(ids)
    rng.shuffle(order)
    return order


def needs(sets: tuple[str, ...]) -> dict[Stratum, int]:
    total: Counter[Stratum] = Counter()
    for name in sets:
        total.update(quotas(SIZES[name]))
    return dict(total)


def shortfalls(candidates: list[Candidate], sets: tuple[str, ...]) -> list[Shortfall]:
    have = Counter((c.role, c.stratum) for c in candidates)
    found = []
    for key, needed in sorted(needs(sets).items()):
        if have[key] < needed:
            found.append(Shortfall(key[0], key[1], needed, have[key]))
    return found


def draw(
    candidates: list[Candidate],
    seed: int,
    version: int,
    sets: tuple[str, ...],
) -> list[Pick]:
    """Pick the emails for `sets`. Raises Short when a stratum runs out."""
    short = shortfalls(candidates, sets)
    if short:
        raise Short(short)
    by_key: dict[Stratum, list[uuid.UUID]] = {}
    for c in candidates:
        by_key.setdefault((c.role, c.stratum), []).append(c.id)
    picks = []
    ordered = [name for name in SET_ORDER if name in sets]
    for key in sorted(by_key):
        order = shuffled(by_key[key], seed, version, key)
        start = 0
        for name in ordered:
            take = quotas(SIZES[name])[key]
            for message_id in order[start : start + take]:
                picks.append(Pick(message_id, name, key[0], key[1]))
            start += take
    return picks


# ---- What is already drawn ----


def latest_versions(session: Session) -> dict[str, int]:
    """The newest drawn set version of each set, for sets drawn at least once."""
    eval_set = AuditEvent.data["eval_set"].astext
    version = AuditEvent.data["set_version"].astext.cast(Integer)
    query = (
        select(eval_set, func.max(version))
        .where(AuditEvent.event_type == PICK_EVENT)
        .group_by(eval_set)
    )
    return {name: top for name, top in session.execute(query)}


def drawn_ids(session: Session) -> set[uuid.UUID]:
    """Every email in any draw, current or earlier."""
    query = select(AuditEvent.subject_id).where(AuditEvent.event_type == PICK_EVENT)
    return {row for row in session.execute(query).scalars() if row is not None}


def current(session: Session) -> Subquery:
    """Emails of the newest draw of each set: message_id, eval_set, set_version."""
    eval_set = AuditEvent.data["eval_set"].astext
    version = AuditEvent.data["set_version"].astext.cast(Integer)
    newest = [
        and_(eval_set == name, version == top) for name, top in latest_versions(session).items()
    ]
    query = select(
        AuditEvent.subject_id.label("message_id"),
        eval_set.label("eval_set"),
        version.label("set_version"),
    ).where(AuditEvent.event_type == PICK_EVENT, or_(false(), *newest))
    return query.subquery("sampled")


def unsampled_labels(session: Session) -> int:
    """Current labels on emails outside every draw (made before sampling existed)."""
    drawn = select(AuditEvent.subject_id).where(AuditEvent.event_type == PICK_EVENT)
    query = (
        select(func.count())
        .select_from(EvalLabel)
        .where(EvalLabel.retired_at.is_(None), EvalLabel.source_message_id.not_in(drawn))
    )
    found: int = session.execute(query).scalar_one()
    return found


# ---- Running a draw ----


@dataclass(frozen=True)
class Result:
    version: int
    sets: tuple[str, ...]
    picks: list[Pick]
    pool: Pool
    written: bool

    def counts(self) -> dict[str, dict[str, int]]:
        """Per set: emails per role and stratum, as "role/stratum"."""
        found: dict[str, dict[str, int]] = {}
        for p in self.picks:
            per_set = found.setdefault(p.eval_set, {})
            key = f"{p.role}/{p.stratum}"
            per_set[key] = per_set.get(key, 0) + 1
        return {name: dict(sorted(v.items())) for name, v in sorted(found.items())}

    def available(self) -> dict[str, int]:
        have = Counter(f"{c.role}/{c.stratum}" for c in self.pool.candidates)
        return dict(sorted(have.items()))


Mode = Literal["first", "redraw-dev"]


def plan(session: Session, mode: Mode) -> tuple[int, tuple[str, ...]]:
    """Which sets to draw and at which version, or a refusal."""
    versions = latest_versions(session)
    if mode == "first":
        if versions:
            drawn = ", ".join(f"{name} v{top}" for name, top in sorted(versions.items()))
            raise DrawError(
                f"already drawn ({drawn}). The test set is fixed; "
                "use --redraw-dev to draw a new dev set"
            )
        return 1, ("dev", "test")
    if "test" not in versions:
        raise DrawError("nothing drawn yet; run without --redraw-dev first")
    return versions.get("dev", 0) + 1, ("dev",)


def run(
    session: Session,
    rules: Rules,
    seed: int,
    mode: Mode,
    write: bool,
    rules_source: str,
) -> Result:
    """Draw, and record the draw when `write` is set. Does not commit."""
    # Held until the caller's transaction ends, so concurrent draws queue up
    session.execute(select(func.pg_advisory_xact_lock(LOCK_KEY)))
    version, sets = plan(session, mode)
    if mode == "first":
        stale = unsampled_labels(session)
        if stale:
            raise DrawError(
                f"{stale} current labels are on emails outside any draw; "
                "decide what happens to them before the first draw"
            )
    found = pool(session, rules, drawn_ids(session))
    picks = draw(found.candidates, seed, version, sets)
    result = Result(version, sets, picks, found, write)
    if write:
        record(session, result, seed, rules_source)
    return result


def record(session: Session, result: Result, seed: int, rules_source: str) -> None:
    data = {
        "set_version": result.version,
        "sets": list(result.sets),
        "seed": seed,
        "rules": rules_source,
        "counts": result.counts(),
        "available": result.available(),
    }
    session.add(AuditEvent(event_type=DRAW_EVENT, actor="user", data=data))
    for p in result.picks:
        pick = {
            "eval_set": p.eval_set,
            "set_version": result.version,
            "role": p.role,
            "stratum": p.stratum,
        }
        event = AuditEvent(
            event_type=PICK_EVENT,
            actor="user",
            subject_type="message",
            subject_id=p.message_id,
            data=pick,
        )
        session.add(event)
    session.flush()
