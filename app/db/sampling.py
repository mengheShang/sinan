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

A dev set version can get one supplement of mail likely to hold a deadline
(Issue #78, ADR 0018): 40 emails that match the keywords or senders of
app/db/boost.py, with no role split, from mail outside every earlier draw.
Its picks join the current dev set version with stratum `deadline_boost`,
so the labeling queue lists them with the original draw and reports can
split them out. The test set is never touched.

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

from app.db import boost as likely
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
# The supplementary dev draw (Issue #78)
BOOST_SIZE = 40
BOOST = likely.STRATUM

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
    # Matches the boost keywords or senders; only computed for a boost draw
    likely: bool = False


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


def pool(
    session: Session,
    rules: Rules,
    exclude: set[uuid.UUID],
    senders: likely.Senders | None = None,
) -> Pool:
    """Candidates outside `exclude`. With `senders`, marks the likely ones."""
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
        hit = senders is not None and likely.likely_deadline(snapshot, senders)
        found.candidates.append(Candidate(message_id, snapshot.role, stratum, hit))
    return found


# ---- The draw (pure) ----


@dataclass(frozen=True)
class Pick:
    message_id: uuid.UUID
    eval_set: str
    role: str
    stratum: str
    # Prefilter outcome of a boost pick, whose stratum is `deadline_boost`
    prefilter: str | None = None


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


def boost_order(ids: list[uuid.UUID], seed: int, version: int) -> list[uuid.UUID]:
    # Its own generator name, so it never repeats the order of a role stratum
    rng = random.Random(f"sinan-boost:{seed}:{version}")  # noqa: S311
    order = sorted(ids)
    rng.shuffle(order)
    return order


def draw_boost(candidates: list[Candidate], seed: int, version: int, size: int) -> list[Pick]:
    """Pick `size` likely candidates for the dev set, with no role split.

    Raises Short when fewer are available: nothing is drawn then.
    """
    hits = [c for c in candidates if c.likely]
    if len(hits) < size:
        raise Short([Shortfall("any", BOOST, size, len(hits))])
    by_id = {c.id: c for c in hits}
    order = boost_order(list(by_id), seed, version)
    picks = []
    for message_id in order[:size]:
        c = by_id[message_id]
        picks.append(Pick(message_id, "dev", c.role, BOOST, c.stratum))
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


def boosted(session: Session, eval_set: str, version: int) -> int:
    """How many boost picks a set version already has."""
    query = select(func.count()).where(
        AuditEvent.event_type == PICK_EVENT,
        AuditEvent.data["eval_set"].astext == eval_set,
        AuditEvent.data["set_version"].astext.cast(Integer) == version,
        AuditEvent.data["stratum"].astext == BOOST,
    )
    found: int = session.execute(query).scalar_one()
    return found


def strata(session: Session, eval_set: str, version: int) -> dict[uuid.UUID, str]:
    """The stratum of each email in one set version: message id to stratum."""
    stratum = AuditEvent.data["stratum"].astext
    query = select(AuditEvent.subject_id, stratum).where(
        AuditEvent.event_type == PICK_EVENT,
        AuditEvent.data["eval_set"].astext == eval_set,
        AuditEvent.data["set_version"].astext.cast(Integer) == version,
    )
    return {row[0]: row[1] for row in session.execute(query) if row[0] is not None}


def drawn_ids(session: Session) -> set[uuid.UUID]:
    """Every email in any draw, current or earlier."""
    query = select(AuditEvent.subject_id).where(AuditEvent.event_type == PICK_EVENT)
    return {row for row in session.execute(query).scalars() if row is not None}


def current(session: Session) -> Subquery:
    """Emails of the newest version of each set: message_id, eval_set, set_version.

    A version's supplement shares its set version, so it is listed too.
    """
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


Mode = Literal["first", "redraw-dev", "boost-dev"]


@dataclass(frozen=True)
class Result:
    version: int
    sets: tuple[str, ...]
    picks: list[Pick]
    pool: Pool
    written: bool
    mode: Mode = "first"

    def counts(self) -> dict[str, dict[str, int]]:
        """Per set: emails per role and stratum, as "role/stratum"."""
        found: dict[str, dict[str, int]] = {}
        for p in self.picks:
            per_set = found.setdefault(p.eval_set, {})
            key = f"{p.role}/{p.stratum}"
            per_set[key] = per_set.get(key, 0) + 1
        return {name: dict(sorted(v.items())) for name, v in sorted(found.items())}

    def available(self) -> dict[str, int]:
        """Candidates per "role/stratum"; for a boost, the likely ones per role."""
        found = self.pool.candidates
        if self.mode == "boost-dev":
            keys = [f"{c.role}/{BOOST}" for c in found if c.likely]
        else:
            keys = [f"{c.role}/{c.stratum}" for c in found]
        have = Counter(keys)
        return dict(sorted(have.items()))


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
        raise DrawError(f"nothing drawn yet; run without --{mode} first")
    if mode == "boost-dev":
        return boost_plan(session, versions), ("dev",)
    return versions.get("dev", 0) + 1, ("dev",)


def boost_plan(session: Session, versions: dict[str, int]) -> int:
    """The dev set version a supplement joins: the current one, once."""
    version = versions.get("dev")
    if version is None:
        raise DrawError("no dev set drawn; use --redraw-dev first")
    if boosted(session, "dev", version):
        raise DrawError(
            f"dev v{version} already has its supplement; "
            "a new one comes only with a new dev set (--redraw-dev)"
        )
    return version


@dataclass(frozen=True)
class Boost:
    """What a boost draw needs: the sender list, where it came from, the size."""

    senders: likely.Senders
    source: str
    size: int = BOOST_SIZE


def run(
    session: Session,
    rules: Rules,
    seed: int,
    mode: Mode,
    write: bool,
    rules_source: str,
    boost: Boost | None = None,
) -> Result:
    """Draw, and record the draw when `write` is set. Does not commit.

    A "boost-dev" draw needs `boost`.
    """
    if mode == "boost-dev" and boost is None:
        raise ValueError("a boost draw needs its sender list")
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
    senders = boost.senders if boost else None
    found = pool(session, rules, drawn_ids(session), senders)
    if boost is not None and mode == "boost-dev":
        picks = draw_boost(found.candidates, seed, version, boost.size)
    else:
        picks = draw(found.candidates, seed, version, sets)
    result = Result(version, sets, picks, found, write, mode)
    if write:
        source = boost.source if boost else None
        record(session, result, seed, rules_source, source)
    return result


def record(
    session: Session,
    result: Result,
    seed: int,
    rules_source: str,
    senders_source: str | None = None,
) -> None:
    data: dict[str, object] = {
        "set_version": result.version,
        "sets": list(result.sets),
        "seed": seed,
        "rules": rules_source,
        "counts": result.counts(),
        "available": result.available(),
    }
    if result.mode == "boost-dev":
        data["stratum"] = BOOST
        data["senders"] = senders_source
    session.add(AuditEvent(event_type=DRAW_EVENT, actor="user", data=data))
    for p in result.picks:
        pick = {
            "eval_set": p.eval_set,
            "set_version": result.version,
            "role": p.role,
            "stratum": p.stratum,
        }
        if p.prefilter is not None:
            pick["prefilter"] = p.prefilter
        event = AuditEvent(
            event_type=PICK_EVENT,
            actor="user",
            subject_type="message",
            subject_id=p.message_id,
            data=pick,
        )
        session.add(event)
    session.flush()
