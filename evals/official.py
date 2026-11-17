"""The official test-set run, its recorded replay and the range runs (Issue #87).

The test set (50 labeled emails, ADR 0012) is read from the label table, like
the dev set, and is used for reported metrics only. Three ways to run it:

- `--set test --live` is the official run. It calls the model once per
  email, stores the raw replies in the local database (`eval_runs`, never
  the repository) and reports. A second official run is refused once one is
  stored, unless `--replace-official` is given; the new run is then the
  official one and the old row is kept. Rows can be deleted, never
  altered: once a set version's rows are deleted, `--recorded` is refused
  and a new official run needs no flag.
- `--set test --recorded` replays the stored official run through the
  current code without calling the model or Gmail, so it gives the same
  numbers. It refuses when the prompt or the rules differ from the run's.
- `--set test --live --range N` runs live N more times and reports only a
  min–max per metric. These runs need a stored official run with the same
  prompt and model, never replace it and are not stored.

Every report starts with a one-line `header`: commit, label set version,
model, prompt version and hash, and the run's date. The commit comes from
`SINAN_COMMIT` (a build arg baked into the backend image by compose), else
from `git` when it is available; an official run refuses an unknown or
dirty commit.

Reports hold fixed keys with counts and metrics only, never an email, a
sample id or a per-email result. Stored replies hold text the model took
from real mail: nothing here logs or prints them.
"""

import argparse
import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import labels, sampling
from app.db.extract import school_addresses
from app.db.models import EvalRun
from app.db.session import get_engine
from app.extraction.link_prompt import LINK_PROMPT_HASH, LINK_PROMPT_VERSION
from app.extraction.prefilter import Rules, load_rules
from app.extraction.prompt import PROMPT_HASH, PROMPT_VERSION
from app.extraction.trust import AUTH_HEADER, with_school
from evals import real, replies
from evals.dataset import Sample, SetError
from evals.real import ReaderFor, RealSet
from evals.run import LiveLinks, LiveReply, evaluate, live_extractor, live_picker, worker_rules

TEST = "test"
UNKNOWN = "unknown"
DIRTY = "-dirty"

# The keys of an official or replayed report, and nothing else
REPORT_KEYS = (
    "header",
    "set",
    "set_version",
    "mode",
    "model",
    "prompt_version",
    "prompt_hash",
    "commit",
    "run_at",
    "counts",
    "metrics",
    "email_groups",
    "linking",
    "sender_trust",
)

# The keys of a range report, and nothing else
RANGE_KEYS = (
    "header",
    "set",
    "set_version",
    "mode",
    "model",
    "prompt_version",
    "prompt_hash",
    "commit",
    "runs",
    "ranges",
)


# ---- Commit and header (pure, except for the git call) ----


def git_head() -> str | None:
    """HEAD of the checkout, with DIRTY appended when the tree has changes."""
    try:
        # Fixed arguments, no user input
        head = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        status = subprocess.run(  # noqa: S603
            ["git", "status", "--porcelain"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = head.stdout.strip()
    if not sha:
        return None
    return sha + DIRTY if status.stdout.strip() else sha


def commit_of(env: Mapping[str, str], git: Callable[[], str | None] = git_head) -> str:
    """The commit the code was built from: SINAN_COMMIT, else git, else UNKNOWN."""
    baked = env.get("SINAN_COMMIT", "").strip()
    if baked and baked != UNKNOWN:
        return baked
    return git() or UNKNOWN


def check_commit(commit: str) -> None:
    """An official run must name the exact, clean commit that made it."""
    if commit == UNKNOWN:
        raise SetError("the commit is unknown; build with SINAN_COMMIT or run from a checkout")
    if commit.endswith(DIRTY):
        raise SetError("the checkout has uncommitted changes; commit them first")


@dataclass(frozen=True)
class RunInfo:
    """What a report's header names."""

    commit: str
    set_version: int
    model: str
    prompt_version: str
    prompt_hash: str
    run_at: datetime


def header(info: RunInfo) -> str:
    """The report's one-line header."""
    day = info.run_at.astimezone(UTC).date().isoformat()
    parts = [
        f"commit {info.commit[:12]}",
        f"labels {TEST} v{info.set_version}",
        f"model {info.model}",
        f"prompt {info.prompt_version} sha256:{info.prompt_hash[:12]}",
        day,
    ]
    return " | ".join(parts)


def info_data(info: RunInfo) -> dict[str, Any]:
    return {
        "header": header(info),
        "set": TEST,
        "set_version": info.set_version,
        "model": info.model,
        "prompt_version": info.prompt_version,
        "prompt_hash": info.prompt_hash,
        "commit": info.commit,
    }


# ---- Reports (pure) ----


def official_report(result: Mapping[str, Any], info: RunInfo, mode: str) -> dict[str, Any]:
    """An official or replayed run's report, with REPORT_KEYS only."""
    found = info_data(info)
    found["mode"] = mode
    found["run_at"] = info.run_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    for key in ("counts", "metrics", "email_groups", "linking", "sender_trust"):
        found[key] = result[key]
    return {key: found[key] for key in REPORT_KEYS}


def ranges(results: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float] | None]:
    """The lowest and highest percent of each metric over the runs.

    A metric with no samples in any run is None.
    """
    if not results:
        raise SetError("a range needs at least one run")
    found: dict[str, dict[str, float] | None] = {}
    for name in results[0]["metrics"]:
        values = [r["metrics"][name]["percent"] for r in results]
        kept = [v for v in values if v is not None]
        found[name] = {"min": min(kept), "max": max(kept)} if kept else None
    return found


def range_report(results: Sequence[Mapping[str, Any]], info: RunInfo) -> dict[str, Any]:
    """The range runs' report, with RANGE_KEYS only."""
    found = info_data(info)
    found["mode"] = "live-range"
    found["runs"] = len(results)
    found["ranges"] = ranges(results)
    return {key: found[key] for key in RANGE_KEYS}


def rules_hash(rules: Rules) -> str:
    """SHA-256 of the effective rules, stable across processes.

    The rules themselves are never stored: the local rules hold real senders.
    """

    def plain(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: plain(v) for k, v in value.items()}
        if isinstance(value, frozenset | set):
            return sorted(value)
        if isinstance(value, tuple | list):
            return [plain(v) for v in value]
        return value

    text = json.dumps(plain(asdict(rules)), sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- The test set and the store ----


def test_version(session: Session) -> int:
    version = sampling.latest_versions(session).get(TEST)
    if version is None:
        raise SetError("no test set has been drawn")
    return version


def db_now(session: Session) -> datetime:
    now: datetime = session.execute(select(func.clock_timestamp())).scalar_one()
    return now


def load_test(session: Session, as_of: datetime | None = None) -> RealSet:
    """The current test set version, as its labels stood at `as_of`."""
    version = test_version(session)
    samples = labels.load_set(session, TEST, set_version=version, as_of=as_of)
    if not samples:
        raise SetError(f"test v{version} has no labels yet")
    sources = labels.sources(session, TEST, version)
    return RealSet(version, samples, {}, sources)


def official(session: Session, version: int) -> EvalRun | None:
    """The stored official run of a test set version: its newest row."""
    query = (
        select(EvalRun)
        .where(EvalRun.eval_set == TEST, EvalRun.set_version == version)
        .order_by(EvalRun.created_at.desc(), EvalRun.id.desc())
        .limit(1)
    )
    return session.execute(query).scalar_one_or_none()


def check_first(session: Session, version: int, replace: bool) -> None:
    """Refuse a second official run unless it is asked for on purpose."""
    if official(session, version) is None or replace:
        return
    raise SetError(
        f"test v{version} already has an official run; "
        "use --recorded to replay it, or --replace-official to make a new one"
    )


def auth_of(samples: Sequence[Sample]) -> dict[str, str]:
    """Each sample's Authentication-Results header, where it has one."""
    found = {}
    for sample in samples:
        value = sample.input.headers.get(AUTH_HEADER)
        if value is not None:
            found[sample.id] = value
    return found


def with_recorded_auth(samples: Sequence[Sample], auth: Mapping[str, str]) -> list[Sample]:
    """The samples with the headers the official run read from Gmail."""
    out = []
    for sample in samples:
        value = auth.get(sample.id)
        out.append(sample if value is None else real.with_header(sample, value))
    return out


@dataclass(frozen=True)
class Recording:
    """What an official run stores. Holds real mail: never logged."""

    labels_as_of: datetime
    model: str
    rules_hash: str
    commit: str
    replies: dict[str, str]
    link_replies: dict[str, str]
    auth: dict[str, str]


def store(session: Session, version: int, rec: Recording) -> EvalRun:
    row = EvalRun(
        eval_set=TEST,
        set_version=version,
        labels_as_of=rec.labels_as_of,
        model=rec.model,
        prompt_version=PROMPT_VERSION,
        prompt_hash=PROMPT_HASH,
        link_prompt_version=LINK_PROMPT_VERSION,
        link_prompt_hash=LINK_PROMPT_HASH,
        rules_hash=rec.rules_hash,
        commit=rec.commit,
        replies=rec.replies,
        link_replies=rec.link_replies,
        auth=rec.auth,
    )
    session.add(row)
    session.flush()
    return row


# ---- Running (the session, model and reader are given) ----


@dataclass(frozen=True)
class Live:
    """What a live run needs, built by the caller after the CI and key checks."""

    rules: Rules
    reply: Callable[[], LiveReply]
    links: Callable[[], LiveLinks]
    reader_for: ReaderFor
    commit: str


def live_samples(session: Session, live: Live) -> tuple[RealSet, list[Sample], Rules, datetime]:
    """The test set as of now, with Gmail's authentication results added."""
    as_of = db_now(session)
    found = load_test(session, as_of)
    rules = with_school(live.rules, school_addresses(session))
    fetched = real.with_auth(session, found, live.reader_for)
    return found, fetched.samples, rules, as_of


def run_official(session: Session, live: Live, replace: bool = False) -> dict[str, Any]:
    """The official run: evaluate live, store the replies, report. Does not commit."""
    check_commit(live.commit)
    version = test_version(session)
    check_first(session, version, replace)
    _, samples, rules, as_of = live_samples(session, live)
    # A token refresh may flag an account; the Worker's sync records that
    session.rollback()
    reply = live.reply()
    links = live.links()
    result = evaluate(TEST, samples, rules, reply, links=links)
    model = reply.extractor.model
    rec = Recording(
        as_of, model, rules_hash(rules), live.commit, reply.raw, links.raw, auth_of(samples)
    )
    row = store(session, version, rec)
    info = RunInfo(live.commit, version, model, PROMPT_VERSION, PROMPT_HASH, row.created_at)
    return official_report(result, info, "live")


def check_replayable(run: EvalRun) -> None:
    if run.prompt_hash != PROMPT_HASH:
        raise SetError(
            f"the official run used prompt {run.prompt_version}; this code has {PROMPT_VERSION}"
        )
    if run.link_prompt_hash != LINK_PROMPT_HASH:
        raise SetError(
            f"the official run used link prompt {run.link_prompt_version}; "
            f"this code has {LINK_PROMPT_VERSION}"
        )


def replay(session: Session, rules: Rules) -> dict[str, Any]:
    """The stored official run through the current code. No model, no Gmail."""
    version = test_version(session)
    run = official(session, version)
    if run is None:
        raise SetError(f"test v{version} has no official run yet; run it with --live first")
    check_replayable(run)
    samples = labels.load_set(session, TEST, set_version=version, as_of=run.labels_as_of)
    rules = with_school(rules, school_addresses(session))
    if rules_hash(rules) != run.rules_hash:
        raise SetError("the prefilter or trust rules differ from the official run's")
    samples = with_recorded_auth(samples, run.auth)
    reply = replies.Recorded(run.replies)
    links = replies.RecordedLinks(run.link_replies)
    result = evaluate(TEST, samples, rules, reply, links=links)
    info = RunInfo(
        run.commit, version, run.model, run.prompt_version, run.prompt_hash, run.created_at
    )
    return official_report(result, info, "recorded")


def run_range(session: Session, live: Live, count: int) -> dict[str, Any]:
    """`count` more live runs, min–max only. Nothing is stored."""
    if count < 1:
        raise SetError("--range needs at least 1 run")
    version = test_version(session)
    run = official(session, version)
    first = live.reply()
    model = first.extractor.model
    if run is None or run.prompt_hash != PROMPT_HASH or run.model != model:
        raise SetError("range runs come after the official run, with its prompt and model")
    _, samples, rules, _ = live_samples(session, live)
    session.rollback()
    results = []
    for n in range(count):
        reply = first if n == 0 else live.reply()
        results.append(evaluate(TEST, samples, rules, reply, links=live.links()))
    info = RunInfo(live.commit, version, model, PROMPT_VERSION, PROMPT_HASH, db_now(session))
    return range_report(results, info)


# ---- The command line (called by evals/run.py) ----


def check_args(args: argparse.Namespace) -> None:
    if args.live == args.recorded:
        raise SetError("the test set needs exactly one of --live and --recorded")
    if args.save_replies or args.save_link_replies:
        raise SetError("replies of real mail are never saved to a file")
    if args.live_links:
        raise SetError("the test set has no --live-links; use --live")
    if args.range is not None and not args.live:
        raise SetError("--range only works with --live")
    if args.replace_official and (args.recorded or args.range is not None):
        raise SetError("--replace-official only works with an official --live run")


def live_setup(args: argparse.Namespace) -> Live:
    """The live model, link step, rules and reader. Checked before reading any mail."""
    env = dict(os.environ)
    extractor = live_extractor(env)
    picker = live_picker(env)
    settings = Settings()
    rules = load_rules(args.rules or worker_rules(settings))
    # Imported here: the Worker's readers need the live OAuth settings
    from apps.worker.sync_job import build_readers

    return Live(
        rules,
        lambda: LiveReply(extractor),
        lambda: LiveLinks(picker),
        build_readers(settings),
        commit_of(env),
    )


def evaluate_test(args: argparse.Namespace) -> dict[str, Any]:
    check_args(args)
    if args.recorded:
        rules = load_rules(args.rules or worker_rules(Settings()))
        with Session(get_engine()) as session:
            return replay(session, rules)
    live = live_setup(args)
    with Session(get_engine()) as session:
        if args.range is not None:
            return run_range(session, live, args.range)
        result = run_official(session, live, args.replace_official)
        session.commit()
    return result
