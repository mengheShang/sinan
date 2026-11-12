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


def found_of(outcome: Outcome) -> list[Found]:
    if outcome.checked is None:
        return []
    items: list[ValidItem] = outcome.checked.items
    found = []
    for n, item in enumerate(items):
        uid = f"{outcome.sample.id}/{n}"
        found.append(Found(uid, item.course, item.title, item.due_at, item.status))
    return found


def to_email(outcome: Outcome) -> Email:
    labels = outcome.sample.labels.items
    return Email(labels, found_of(outcome))


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
    emails = [to_email(o) for o in outcomes]
    scores = score(emails)
    return {
        "set": set_name,
        "mode": mode,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "counts": count_data(outcomes, scores),
        "metrics": scores_data(scores),
    }
