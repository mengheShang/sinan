"""Match extracted items to labeled ones and score them. Pure functions only.

The matching rule is written out in evals/README.md ("Matching rule"). In
short: an extracted item can only match a labeled item from the same email,
their titles, without the course code, must share at least half of their
words, and each item is used at most once, best pairs first.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from evals.dataset import ItemLabel

# Two titles match when their word sets overlap by at least this much
MIN_OVERLAP = 0.5

WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Found:
    """One extracted item after validation, as the evaluation sees it."""

    # Unique in a run, such as "<sample id>/0". Never reported.
    uid: str
    course: str | None
    title: str
    due_at: datetime | None
    status: str
    # The uid of an earlier found item this one changes, once matching links
    # changes (ADR 0009, Issue 6). None means "not linked".
    target: str | None = None


@dataclass(frozen=True)
class Email:
    """The labeled items and the found items of one email."""

    labels: Sequence[ItemLabel]
    found: Sequence[Found]


@dataclass(frozen=True)
class Ratio:
    hits: int
    total: int

    @property
    def value(self) -> float | None:
        """None when there is nothing to measure, instead of dividing by zero."""
        if self.total == 0:
            return None
        return self.hits / self.total


@dataclass(frozen=True)
class Scores:
    emails: int
    labeled: int
    found: int
    matched: int
    recall: Ratio
    precision: Ratio
    due_accuracy: Ratio
    change_accuracy: Ratio


def words(title: str) -> frozenset[str]:
    lowered = title.casefold()
    return frozenset(WORD.findall(lowered))


def overlap(a: str, b: str, ignored: frozenset[str] = frozenset()) -> float:
    """Shared words over all words (Jaccard). 0 when either title has no words left."""
    left = words(a) - ignored
    right = words(b) - ignored
    union = left | right
    if not left or not right:
        return 0.0
    return len(left & right) / len(union)


def course_words(label: ItemLabel, item: Found) -> frozenset[str]:
    """Words of either course, so "MATH 005 midterm" can match "Midterm exam"."""
    return words(label.course or "") | words(item.course or "")


def pair_score(label: ItemLabel, item: Found) -> float:
    ignored = course_words(label, item)
    return overlap(label.title, item.title, ignored)


def match_email(email: Email) -> list[tuple[ItemLabel, Found]]:
    """One-to-one pairs, highest overlap first. Ties keep the listed order."""
    scored = []
    for i, label in enumerate(email.labels):
        for j, item in enumerate(email.found):
            score = pair_score(label, item)
            if score >= MIN_OVERLAP:
                scored.append((-score, i, j))
    scored.sort()
    used_labels: set[int] = set()
    used_found: set[int] = set()
    pairs = []
    for _, i, j in scored:
        if i in used_labels or j in used_found:
            continue
        used_labels.add(i)
        used_found.add(j)
        pairs.append((email.labels[i], email.found[j]))
    return pairs


def due_correct(label: ItemLabel, item: Found) -> bool:
    """The exact time when the label has one; otherwise no time at all.

    An ambiguous due time must also leave the item in needs_review.
    """
    if label.due_at is not None:
        return item.due_at == label.due_at
    if item.due_at is not None:
        return False
    if label.due_ambiguous:
        return item.status == "needs_review"
    return True


def change_correct(label: ItemLabel, item: Found, ref_of: dict[str, str]) -> bool:
    """The found item links to the found item that was matched to the changed label."""
    if item.target is None:
        return False
    return ref_of.get(item.target) == label.changes


def score(emails: Sequence[Email]) -> Scores:
    pairs = []
    for email in emails:
        pairs.extend(match_email(email))
    labeled = sum(len(e.labels) for e in emails)
    found = sum(len(e.found) for e in emails)
    matched = len(pairs)
    # Which labeled ref each matched found item stands for, to check links
    ref_of = {item.uid: label.ref for label, item in pairs}
    due_hits = sum(due_correct(label, item) for label, item in pairs)
    changes = [(label, item) for label, item in pairs if label.changes is not None]
    change_hits = sum(change_correct(label, item, ref_of) for label, item in changes)
    labeled_changes = sum(1 for e in emails for label in e.labels if label.changes is not None)
    return Scores(
        emails=len(emails),
        labeled=labeled,
        found=found,
        matched=matched,
        recall=Ratio(matched, labeled),
        precision=Ratio(matched, found),
        due_accuracy=Ratio(due_hits, matched),
        change_accuracy=Ratio(change_hits, labeled_changes),
    )
