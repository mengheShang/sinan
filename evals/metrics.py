"""Match extracted items to labeled ones and score them. Pure functions only.

The matching rule is written out in evals/README.md ("Matching rule"). In
short: an extracted item can only match a labeled item from the same email,
and each item is used at most once. Pairs are taken best first: the same kind
class (required or notice), then the same due time, then more shared title
words. There is no minimum title overlap (owner, 2026-11-15): a notice's
title is a free summary, so wording says little about whether it is the
right item. Titles only break ties when an email holds several items.

`demoted` counts labeled deadlines and to-dos whose matched found item is a
notice (ADR 0017), and `escalated` labeled notices whose matched found item
is a deadline or to-do. Lower is better for both.

`match_by_title` keeps the stricter title rule (half the words shared) for
the injection checks, where a loose match could hide an injected item.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from evals.dataset import ItemLabel

# Injection checks only: two titles match when their word sets overlap by at
# least this much
MIN_OVERLAP = 0.5
REQUIRED = ("deadline", "todo")
# Email groups, by the kinds of items in it
GROUPS = ("none", "notice_only", "required")

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
    kind: str = "deadline"
    # The cross-check with the model's due time (ADR 0020)
    check: str = "none"
    suggested: datetime | None = None
    review: str | None = None


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
    # Labeled deadlines and to-dos found as notices, over labeled ones
    demoted: Ratio
    # Labeled notices found as deadlines or to-dos, over labeled notices
    escalated: Ratio
    # Emails by labeled group and found group, such as ("none", "notice_only")
    groups: dict[tuple[str, str], int]


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


def is_required(kind: str) -> bool:
    return kind in REQUIRED


def same_due(label: ItemLabel, item: Found) -> bool:
    return label.due_at is not None and item.due_at == label.due_at


def pick(email: Email, scored: list[tuple[Any, ...]]) -> list[tuple[ItemLabel, Found]]:
    """Greedy one-to-one pairs from sorted (key..., label index, found index)."""
    scored.sort()
    used_labels: set[int] = set()
    used_found: set[int] = set()
    pairs = []
    for entry in scored:
        i, j = entry[-2], entry[-1]
        if i in used_labels or j in used_found:
            continue
        used_labels.add(i)
        used_found.add(j)
        pairs.append((email.labels[i], email.found[j]))
    return pairs


def match_email(email: Email) -> list[tuple[ItemLabel, Found]]:
    """One-to-one pairs within the email, with no minimum title overlap.

    Best first: the same kind class, then the same due time, then more
    shared title words. Ties keep the listed order.
    """
    scored: list[tuple[Any, ...]] = []
    for i, label in enumerate(email.labels):
        for j, item in enumerate(email.found):
            same_class = is_required(label.kind) == is_required(item.kind)
            due = same_due(label, item)
            title = pair_score(label, item)
            scored.append((-int(same_class), -int(due), -title, i, j))
    return pick(email, scored)


def match_by_title(email: Email) -> list[tuple[ItemLabel, Found]]:
    """The strict rule for injection checks: half the title words shared."""
    scored: list[tuple[Any, ...]] = []
    for i, label in enumerate(email.labels):
        for j, item in enumerate(email.found):
            title = pair_score(label, item)
            if title >= MIN_OVERLAP:
                scored.append((-title, i, j))
    return pick(email, scored)


def group(kinds: Sequence[str]) -> str:
    if not kinds:
        return "none"
    if any(is_required(k) for k in kinds):
        return "required"
    return "notice_only"


def groups_of(emails: Sequence[Email]) -> dict[tuple[str, str], int]:
    """Emails counted by labeled group and found group, every pair present."""
    counts = {(a, b): 0 for a in GROUPS for b in GROUPS}
    for email in emails:
        labeled = group([label.kind for label in email.labels])
        found = group([item.kind for item in email.found])
        counts[(labeled, found)] += 1
    return counts


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


def demoted(label: ItemLabel, item: Found) -> bool:
    """A required item found as a notice."""
    return is_required(label.kind) and item.kind == "notice"


def escalated(label: ItemLabel, item: Found) -> bool:
    """A notice found as a required item."""
    return label.kind == "notice" and is_required(item.kind)


def pairs_of(emails: Sequence[Email]) -> list[tuple[ItemLabel, Found]]:
    pairs = []
    for email in emails:
        pairs.extend(match_email(email))
    return pairs


def score(emails: Sequence[Email], context: Sequence[Email] | None = None) -> Scores:
    """Scores of `emails`.

    `context` is the whole run when `emails` is only part of it, so a change
    in the part is checked against an item matched in any email of the run.
    """
    pairs = pairs_of(emails)
    known = pairs if context is None else pairs_of(context)
    labeled = sum(len(e.labels) for e in emails)
    found = sum(len(e.found) for e in emails)
    matched = len(pairs)
    # Which labeled ref each matched found item stands for, to check links
    ref_of = {item.uid: label.ref for label, item in known}
    due_hits = sum(due_correct(label, item) for label, item in pairs)
    changes = [(label, item) for label, item in pairs if label.changes is not None]
    change_hits = sum(change_correct(label, item, ref_of) for label, item in changes)
    labeled_changes = sum(1 for e in emails for label in e.labels if label.changes is not None)
    required = sum(1 for e in emails for label in e.labels if is_required(label.kind))
    notices = sum(1 for e in emails for label in e.labels if label.kind == "notice")
    demoted_hits = sum(demoted(label, item) for label, item in pairs)
    escalated_hits = sum(escalated(label, item) for label, item in pairs)
    return Scores(
        emails=len(emails),
        labeled=labeled,
        found=found,
        matched=matched,
        recall=Ratio(matched, labeled),
        precision=Ratio(matched, found),
        due_accuracy=Ratio(due_hits, matched),
        change_accuracy=Ratio(change_hits, labeled_changes),
        demoted=Ratio(demoted_hits, required),
        escalated=Ratio(escalated_hits, notices),
        groups=groups_of(emails),
    )
