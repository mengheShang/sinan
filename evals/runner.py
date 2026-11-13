"""Run each sample through the production pipeline and build a summary report.

The path is the one the Worker takes (app/db/extract.py): the prefilter, the
prepared text, the model, and deterministic validation. Only where the reply
comes from changes: a recorded reply, or a live model call.

Pure except for the `reply` callable it is given. The report holds counts and
metrics only, never an email, a subject, a sender, an id or a per-email result
(ADR 0009, decision 5).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.extraction.prefilter import Envelope, Rules, decide
from app.extraction.prompt import PROMPT_VERSION
from app.extraction.validate import Checked, ValidItem, check
from app.items.matching import Known, match
from evals.dataset import Sample
from evals.metrics import Email, Found, Ratio, Scores, score

# Gives the raw model reply for one sample that passed the prefilter
Reply = Callable[[Sample], str]


@dataclass(frozen=True)
class Outcome:
    """What the pipeline did with one sample. Kept in memory, never reported."""

    sample: Sample
    skipped: bool
    checked: Checked | None


def envelope(sample: Sample) -> Envelope:
    snap = sample.input
    return Envelope(snap.role, snap.from_addr, snap.subject, snap.headers)


def process(sample: Sample, rules: Rules, reply: Reply) -> Outcome:
    decision = decide(envelope(sample), rules)
    if decision.action == "skip":
        return Outcome(sample, True, None)
    raw = reply(sample)
    snap = sample.input
    checked = check(raw, snap.body_text, snap.sent_at)
    return Outcome(sample, False, checked)


def in_order(samples: Sequence[Sample]) -> list[Sample]:
    """Oldest first, as the Worker sees mail, so a change comes after its target."""
    return sorted(samples, key=lambda s: (s.input.sent_at, s.id))


def run(samples: Sequence[Sample], rules: Rules, reply: Reply) -> list[Outcome]:
    ordered = in_order(samples)
    return [process(sample, rules, reply) for sample in ordered]


class Linker:
    """Links each change to the earlier item it changes, as extraction does.

    Uses the production matcher (app/items/matching.py) over the items found
    so far. An item stands for one tracked item; a change linked to it
    becomes that item's newest found item, so the next change in a chain
    links to it. An update the matcher cannot place is kept as a new item,
    as extraction saves it.
    """

    def __init__(self) -> None:
        self.pool: list[Known[int]] = []
        # Newest found uid of each tracked item, and the sample it came from
        self.latest: list[str] = []
        self.origin: list[str] = []

    def add(self, uid: str, sample_id: str, item: ValidItem) -> None:
        key = len(self.latest)
        self.pool.append(Known(key, item.course, item.title))
        self.latest.append(uid)
        self.origin.append(sample_id)

    def link(self, uid: str, sample_id: str, item: ValidItem) -> str | None:
        """The uid this item changes, or None after adding it as a new item."""
        if item.change_type == "update" and item.due_at is not None:
            earlier = [k for k in self.pool if self.origin[k.key] != sample_id]
            found = match(item.course, item.title, earlier)
            if found.target is not None:
                target = self.latest[found.target]
                self.latest[found.target] = uid
                return target
        self.add(uid, sample_id, item)
        return None


def found_of(outcome: Outcome, linker: Linker | None = None) -> list[Found]:
    """The found items of one email. Without a linker, nothing is linked."""
    if outcome.checked is None:
        return []
    if linker is None:
        linker = Linker()
    items: list[ValidItem] = outcome.checked.items
    sample_id = outcome.sample.id
    found = []
    for n, item in enumerate(items):
        uid = f"{sample_id}/{n}"
        target = linker.link(uid, sample_id, item)
        found.append(Found(uid, item.course, item.title, item.due_at, item.status, target))
    return found


def to_emails(outcomes: Sequence[Outcome]) -> list[Email]:
    """Each email's labels and found items. Outcomes must be oldest first."""
    linker = Linker()
    return [Email(o.sample.labels.items, found_of(o, linker)) for o in outcomes]


def ratio_data(ratio: Ratio) -> dict[str, Any]:
    value = ratio.value
    rounded = None if value is None else round(value * 100, 2)
    return {"percent": rounded, "hits": ratio.hits, "total": ratio.total}


def scores_data(scores: Scores) -> dict[str, Any]:
    return {
        "item_recall": ratio_data(scores.recall),
        "item_precision": ratio_data(scores.precision),
        "due_accuracy": ratio_data(scores.due_accuracy),
        "change_accuracy": ratio_data(scores.change_accuracy),
    }


def count_data(outcomes: Sequence[Outcome], scores: Scores) -> dict[str, int]:
    skipped = sum(o.skipped for o in outcomes)
    invalid = sum(1 for o in outcomes if o.checked and o.checked.status == "invalid")
    return {
        "emails": scores.emails,
        "prefilter_skipped": skipped,
        "invalid_replies": invalid,
        "labeled_items": scores.labeled,
        "found_items": scores.found,
        "matched_items": scores.matched,
    }


def report(set_name: str, mode: str, model: str, outcomes: Sequence[Outcome]) -> dict[str, Any]:
    """Summary metrics and counts only."""
    emails = to_emails(outcomes)
    scores = score(emails)
    return {
        "set": set_name,
        "mode": mode,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "counts": count_data(outcomes, scores),
        "metrics": scores_data(scores),
    }
