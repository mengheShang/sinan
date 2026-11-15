"""Find the earlier item a deadline change refers to. Pure functions only.

The technical design ("DDL changes") matches on course and normalized title,
and only exactly one high-confidence candidate becomes a proposal; several
candidates or none mean review. The rule here:

- Courses must be equal after normalizing: Unicode NFKC, case folded, and
  only letters and digits kept, so "CS 000", "cs000" and "CS-000" agree.
  An item without a course matches only items without a course.
- A title becomes its set of words (runs of letters and digits, case
  folded), minus the words of its course, so "MATH 005 midterm" and
  "Midterm exam" compare as {midterm} and {midterm, exam}.
- Two titles are similar when both keep at least one word, they hold the
  same numbers ("Homework 3" never matches "Homework 4"), and they share at
  least half of all their words (Jaccard overlap of 0.5 or more).
- A candidate is an earlier, active item with an equal course and a similar
  title. Exactly one candidate is a high-confidence match.

When there is not exactly one, extraction may ask the model to pick one
(ADR 0019). `choices` builds the list it may pick from, in deterministic
code and never from the model: the items with an equal course whose newest
email is at most WINDOW older than the change, or whose due time is still
ahead, newest first, at most MAX_CHOICES of them.

The evaluation runner uses the same functions, so its change accuracy
measures this rule.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

# Same threshold as the evaluation's item matching (evals/README.md)
MIN_OVERLAP = 0.5

# Letters and digits in any script; "_" is not a letter
WORD = re.compile(r"[^\W_]+")

Outcome = Literal["one", "several", "none"]

# The model's choices for a change (ADR 0019): items seen this recently or
# still ahead, at most this many
WINDOW = timedelta(days=60)
MAX_CHOICES = 15


@dataclass(frozen=True)
class Known[Key]:
    """An earlier item that a change could refer to."""

    key: Key
    course: str | None
    title: str
    # The current due time, and the date of the newest email behind it
    due_at: datetime | None = None
    seen_at: datetime | None = None


@dataclass(frozen=True)
class Match[Key]:
    outcome: Outcome
    # The target when outcome is "one", else None
    target: Key | None
    candidates: int


def fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def course_key(course: str | None) -> str | None:
    """The course with only letters and digits, case folded; None when empty."""
    if course is None:
        return None
    joined = "".join(WORD.findall(fold(course)))
    return joined or None


def words(text: str) -> frozenset[str]:
    return frozenset(WORD.findall(fold(text)))


def title_words(title: str, course: str | None) -> frozenset[str]:
    """Title words without the course's words, written apart or together."""
    ignored = set(words(course or ""))
    key = course_key(course)
    if key is not None:
        ignored.add(key)
    return words(title) - ignored


def numbers(found: frozenset[str]) -> frozenset[str]:
    return frozenset(w for w in found if any(c.isdigit() for c in w))


def similar(left: frozenset[str], right: frozenset[str]) -> bool:
    if not left or not right:
        return False
    if numbers(left) != numbers(right):
        return False
    shared = len(left & right)
    total = len(left | right)
    return shared / total >= MIN_OVERLAP


def is_candidate[Key](course: str | None, title: str, known: Known[Key]) -> bool:
    if course_key(course) != course_key(known.course):
        return False
    mine = title_words(title, course)
    theirs = title_words(known.title, known.course)
    return similar(mine, theirs)


def match[Key](course: str | None, title: str, pool: Sequence[Known[Key]]) -> Match[Key]:
    """The one candidate in `pool` for a change, or how many there were."""
    found = [k for k in pool if is_candidate(course, title, k)]
    if len(found) == 1:
        return Match("one", found[0].key, 1)
    if found:
        return Match("several", None, len(found))
    return Match("none", None, 0)


def is_recent[Key](known: Known[Key], sent_at: datetime) -> bool:
    """Seen at most WINDOW before `sent_at`, or still due at or after it."""
    if known.seen_at is not None and known.seen_at >= sent_at - WINDOW:
        return True
    return known.due_at is not None and known.due_at >= sent_at


def newest_first[Key](known: Known[Key]) -> float:
    if known.seen_at is None:
        return 0.0
    return -known.seen_at.timestamp()


def choices[Key](
    course: str | None, sent_at: datetime, pool: Sequence[Known[Key]]
) -> list[Known[Key]]:
    """The items the model may link a change to (ADR 0019).

    An equal course, recent as of `sent_at` (the change email's date),
    newest first, at most MAX_CHOICES. `pool` holds the active items from
    other emails, as for `match`.
    """
    key = course_key(course)
    same = [k for k in pool if course_key(k.course) == key and is_recent(k, sent_at)]
    ordered = sorted(same, key=newest_first)
    return ordered[:MAX_CHOICES]
