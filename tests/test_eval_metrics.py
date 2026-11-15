"""Metrics on hand-made labeled and found items, including empty sets."""

from datetime import UTC, datetime

from evals.dataset import ItemLabel
from evals.metrics import Email, Found, Ratio, match_by_title, match_email, overlap, score

DUE = datetime(2026, 11, 14, 7, 59, tzinfo=UTC)
OTHER = datetime(2026, 11, 15, 7, 59, tzinfo=UTC)


def label(
    ref: str,
    title: str,
    due: datetime | None = DUE,
    ambiguous: bool = False,
    changes: str | None = None,
    course: str | None = "CS 000",
    kind: str = "deadline",
) -> ItemLabel:
    raw = "Friday" if due is not None or ambiguous else None
    # The label format takes UTC times as text ending in Z
    due_text = due.strftime("%Y-%m-%dT%H:%M:%SZ") if due else None
    data = {
        "ref": ref,
        "kind": kind,
        "course": course,
        "title": title,
        "due_raw": raw,
        "due_at": due_text,
        "due_ambiguous": ambiguous,
        "changes": changes,
    }
    return ItemLabel.model_validate(data)


def found(
    uid: str,
    title: str,
    due: datetime | None = DUE,
    status: str = "open",
    target: str | None = None,
    course: str | None = "CS 000",
    kind: str = "deadline",
) -> Found:
    return Found(uid, course, title, due, status, target, kind)


def test_ratio_of_nothing_is_none() -> None:
    assert Ratio(0, 0).value is None
    assert Ratio(1, 4).value == 0.25


def test_empty_set_divides_by_nothing() -> None:
    scores = score([])
    assert scores.emails == 0
    assert scores.recall.value is None
    assert scores.precision.value is None
    assert scores.due_accuracy.value is None
    assert scores.change_accuracy.value is None


def test_emails_without_items() -> None:
    scores = score([Email([], []), Email([], [])])
    assert scores.emails == 2
    assert scores.recall == Ratio(0, 0)
    assert scores.precision == Ratio(0, 0)
    assert scores.due_accuracy == Ratio(0, 0)


def test_nothing_found() -> None:
    scores = score([Email([label("a", "Homework 3")], [])])
    assert scores.recall == Ratio(0, 1)
    assert scores.precision == Ratio(0, 0)
    assert scores.due_accuracy == Ratio(0, 0)


def test_nothing_labeled() -> None:
    scores = score([Email([], [found("x", "Homework 3")])])
    assert scores.recall == Ratio(0, 0)
    assert scores.precision == Ratio(0, 1)


def test_recall_and_precision() -> None:
    first = Email([label("a", "Homework 3"), label("b", "Lab 5")], [found("x", "Homework 3")])
    second = Email([], [found("y", "Read the notes")])
    scores = score([first, second])
    assert scores.matched == 1
    assert scores.recall == Ratio(1, 2)
    assert scores.precision == Ratio(1, 2)


def test_items_only_match_in_their_own_email() -> None:
    first = Email([label("a", "Homework 3")], [])
    second = Email([], [found("x", "Homework 3")])
    scores = score([first, second])
    assert scores.matched == 0


def test_overlap_of_titles() -> None:
    assert overlap("Midterm exam", "Midterm") == 0.5
    assert overlap("Homework 3", "homework #3") == 1.0
    assert overlap("Homework 3", "Lab 5") == 0.0
    assert overlap("", "Lab 5") == 0.0


def test_course_words_do_not_count_in_title_rule() -> None:
    labels = [label("a", "Midterm exam", course="MATH 005")]
    items = [found("x", "MATH 005 midterm", course=None)]
    assert len(match_by_title(Email(labels, items))) == 1


def test_title_that_is_only_the_course_matches_nothing_in_title_rule() -> None:
    labels = [label("a", "CS 000")]
    items = [found("x", "CS 000")]
    assert match_by_title(Email(labels, items)) == []


def test_each_item_is_used_once_best_first_in_title_rule() -> None:
    labels = [label("a", "Problem Set 4"), label("b", "Problem Set 4 reading")]
    items = [found("x", "Problem Set 4 reading")]
    pairs = match_by_title(Email(labels, items))
    assert [(lab.ref, item.uid) for lab, item in pairs] == [("b", "x")]


def test_due_accuracy() -> None:
    labels = [label("a", "Homework 3"), label("b", "Lab 5")]
    items = [found("x", "Homework 3"), found("y", "Lab 5", due=OTHER)]
    scores = score([Email(labels, items)])
    assert scores.due_accuracy == Ratio(1, 2)


def test_ambiguous_due_must_need_review() -> None:
    labels = [
        label("a", "Homework 3", due=None, ambiguous=True),
        label("b", "Lab 5", due=None, ambiguous=True),
        label("c", "Lab 6", due=None, ambiguous=True),
    ]
    items = [
        found("x", "Homework 3", due=None, status="needs_review"),
        found("y", "Lab 5", due=None, status="open"),
        found("z", "Lab 6", due=DUE, status="open"),
    ]
    scores = score([Email(labels, items)])
    assert scores.due_accuracy == Ratio(1, 3)


def test_no_due_time_must_find_none() -> None:
    labels = [label("a", "Read chapter 8", due=None), label("b", "Read chapter 9", due=None)]
    items = [found("x", "Read chapter 8", due=None), found("y", "Read chapter 9")]
    scores = score([Email(labels, items)])
    assert scores.due_accuracy == Ratio(1, 2)


def test_change_linked_to_the_right_item() -> None:
    first = Email([label("hw3", "Homework 3")], [found("x", "Homework 3")])
    changed = label("hw3-v2", "Homework 3", due=OTHER, changes="hw3")
    second = Email([changed], [found("y", "Homework 3", due=OTHER, target="x")])
    scores = score([first, second])
    assert scores.change_accuracy == Ratio(1, 1)


def test_change_linked_to_the_wrong_item_or_not_at_all() -> None:
    first = Email(
        [label("hw3", "Homework 3"), label("lab5", "Lab 5")],
        [found("x", "Homework 3"), found("w", "Lab 5")],
    )
    wrong = Email(
        [label("hw3-v2", "Homework 3", changes="hw3")],
        [found("y", "Homework 3", target="w")],
    )
    unlinked = Email([label("lab5-v2", "Lab 5", changes="lab5")], [found("z", "Lab 5")])
    scores = score([first, wrong, unlinked])
    assert scores.change_accuracy == Ratio(0, 2)


def test_missed_change_counts_as_wrong() -> None:
    first = Email([label("hw3", "Homework 3")], [found("x", "Homework 3")])
    second = Email([label("hw3-v2", "Homework 3", changes="hw3")], [])
    scores = score([first, second])
    assert scores.change_accuracy == Ratio(0, 1)


def test_demoted_counts_required_items_found_as_notices() -> None:
    labels = [
        label("hw", "Homework 3"),
        label("read", "Read chapter 8", due=None, kind="todo"),
        label("talk", "Guest talk", kind="notice"),
        label("lab", "Lab 5"),
    ]
    items = [
        found("a", "Homework 3", kind="notice"),
        found("b", "Read chapter 8", due=None, kind="todo"),
        found("c", "Guest talk", kind="notice"),
    ]
    scores = score([Email(labels, items)])
    # Only Homework 3; the missed Lab 5 counts against recall, not here
    assert scores.demoted == Ratio(1, 3)
    assert scores.recall == Ratio(3, 4)


def test_a_notice_found_as_a_deadline_is_not_demoted() -> None:
    labels = [label("talk", "Guest talk", kind="notice")]
    scores = score([Email(labels, [found("a", "Guest talk")])])
    assert scores.demoted == Ratio(0, 0)
    assert scores.demoted.value is None
    assert scores.escalated == Ratio(1, 1)


# ---- Matching without a title threshold (owner, 2026-11-15) ----


def test_a_notice_matches_whatever_its_title() -> None:
    labels = [label("d", "Piazza daily digest 10/17", due=None, kind="notice")]
    items = [found("x", "CSCI 455 forum: question about Lab 9", due=None, kind="notice")]
    assert len(match_email(Email(labels, items))) == 1
    assert score([Email(labels, items)]).recall == Ratio(1, 1)


def test_two_required_items_pair_by_due_time() -> None:
    labels = [label("a", "Homework 3"), label("b", "Project proposal", due=OTHER)]
    items = [found("x", "Submit the proposal", due=OTHER), found("y", "HW three")]
    pairs = match_email(Email(labels, items))
    assert sorted((lab.ref, item.uid) for lab, item in pairs) == [("a", "y"), ("b", "x")]


def test_same_kind_class_comes_before_title() -> None:
    labels = [label("n", "Guest talk", due=None, kind="notice"), label("h", "Homework 3")]
    items = [found("x", "Homework 3 guest talk", due=None, kind="notice"), found("y", "HW")]
    pairs = match_email(Email(labels, items))
    assert sorted((lab.ref, item.uid) for lab, item in pairs) == [("h", "y"), ("n", "x")]


def test_extra_items_stay_unmatched() -> None:
    labels = [label("n", "Newsletter", due=None, kind="notice")]
    items = [
        found("x", "Event one", due=None, kind="notice"),
        found("y", "Event two", due=None, kind="notice"),
    ]
    scores = score([Email(labels, items)])
    assert scores.recall == Ratio(1, 1)
    assert scores.precision == Ratio(1, 2)


def test_email_groups() -> None:
    notice = label("n", "Talk", due=None, kind="notice")
    emails = [
        Email([], []),
        Email([], [found("x", "Sale", due=None, kind="notice")]),
        Email([notice], [found("y", "Talk", kind="todo")]),
        Email([label("h", "Homework 3")], [found("z", "Homework 3")]),
    ]
    groups = score(emails).groups
    assert groups[("none", "none")] == 1
    assert groups[("none", "notice_only")] == 1
    assert groups[("notice_only", "required")] == 1
    assert groups[("required", "required")] == 1
    assert sum(groups.values()) == 4
