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

The evaluation runner uses the same functions, so its change accuracy
measures this rule.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

# Same threshold as the evaluation's item matching (evals/README.md)
MIN_OVERLAP = 0.5

# Letters and digits in any script; "_" is not a letter
WORD = re.compile(r"[^\W_]+")

Outcome = Literal["one", "several", "none"]


@dataclass(frozen=True)
class Known[Key]:
    """An earlier item that a change could refer to."""

    key: Key
    course: str | None
    title: str


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
