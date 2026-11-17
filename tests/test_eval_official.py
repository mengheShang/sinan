"""The official test-set run's pure parts (Issue #87): header, ranges, reports, commit.

No database, no network. Every value is fictional.
"""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.extraction.prefilter import EXAMPLE_RULES, Rules, RuleSet, load_rules
from app.extraction.trust import AUTH_HEADER
from evals import dataset, official, replies
from evals.dataset import SetError
from evals.official import RunInfo
from evals.run import SETS, evaluate, main

WHEN = datetime(2026, 11, 20, 18, 30, tzinfo=UTC)
COMMIT = "0123456789abcdef0123456789abcdef01234567"
HASH = "ab" * 32
INFO = RunInfo(COMMIT, 1, "claude-fable-5-2", "v7", HASH, WHEN)


def synthetic_report() -> dict[str, Any]:
    files = SETS["synthetic"]
    reply = replies.Recorded(replies.load(files.replies))
    links = replies.RecordedLinks(replies.load_links(files.links))
    rules = load_rules(EXAMPLE_RULES)
    return evaluate("synthetic", files.load(), rules, reply, links=links)


def with_percent(result: dict[str, Any], name: str, value: float | None) -> dict[str, Any]:
    metrics = {k: dict(v) for k, v in result["metrics"].items()}
    metrics[name]["percent"] = value
    return {**result, "metrics": metrics}


# ---- Header ----


def test_header_is_one_line_with_every_field() -> None:
    line = official.header(INFO)
    assert line == (
        "commit 0123456789ab | labels test v1 | model claude-fable-5-2"
        " | prompt v7 sha256:abababababab | 2026-11-20"
    )
    assert "\n" not in line


def test_header_date_is_utc() -> None:
    late = datetime.fromisoformat("2026-11-20T23:30:00-08:00")
    info = RunInfo(COMMIT, 1, "m", "v7", HASH, late)
    assert official.header(info).endswith("2026-11-21")


# ---- Commit ----


def test_commit_prefers_the_baked_value() -> None:
    found = official.commit_of({"SINAN_COMMIT": COMMIT}, lambda: "other")
    assert found == COMMIT


def test_commit_falls_back_to_git_then_unknown() -> None:
    assert official.commit_of({"SINAN_COMMIT": "unknown"}, lambda: "abc123") == "abc123"
    assert official.commit_of({}, lambda: None) == official.UNKNOWN


@pytest.mark.parametrize("commit", [official.UNKNOWN, COMMIT + official.DIRTY])
def test_official_run_needs_a_clean_known_commit(commit: str) -> None:
    with pytest.raises(SetError):
        official.check_commit(commit)


def test_clean_commit_passes() -> None:
    official.check_commit(COMMIT)


# ---- Reports hold fixed keys only ----


def test_official_report_holds_fixed_keys_only() -> None:
    result = synthetic_report()
    found = official.official_report(result, INFO, "live")
    assert tuple(found) == official.REPORT_KEYS
    assert found["header"] == official.header(INFO)
    assert found["run_at"] == "2026-11-20T18:30:00Z"
    text = json.dumps(found)
    # No sample id, so no per-email result
    assert "syn-" not in text


def test_range_report_holds_fixed_keys_only() -> None:
    result = synthetic_report()
    found = official.range_report([result, result], INFO)
    assert tuple(found) == official.RANGE_KEYS
    assert found["runs"] == 2
    assert set(found["ranges"]) == set(result["metrics"])
    for value in found["ranges"].values():
        assert value is None or set(value) == {"min", "max"}


# ---- Range aggregation ----


def test_ranges_take_min_and_max_per_metric() -> None:
    base = synthetic_report()
    low = with_percent(base, "item_recall", 80.0)
    high = with_percent(base, "item_recall", 92.5)
    mid = with_percent(base, "item_recall", 85.0)
    found = official.ranges([mid, high, low])
    assert found["item_recall"] == {"min": 80.0, "max": 92.5}


def test_ranges_skip_runs_without_samples() -> None:
    base = synthetic_report()
    some = with_percent(base, "due_accuracy", 70.0)
    none = with_percent(base, "due_accuracy", None)
    assert official.ranges([some, none])["due_accuracy"] == {"min": 70.0, "max": 70.0}
    assert official.ranges([none, none])["due_accuracy"] is None


def test_ranges_need_a_run() -> None:
    with pytest.raises(SetError):
        official.ranges([])


# ---- Rules hash and recorded headers ----


def test_rules_hash_is_stable_and_sensitive() -> None:
    rules = load_rules(EXAMPLE_RULES)
    first = official.rules_hash(rules)
    assert first == official.rules_hash(load_rules(EXAMPLE_RULES))
    other = Rules(trust=RuleSet(domains=frozenset({"example.edu", "example.org"})))
    same = Rules(trust=RuleSet(domains=frozenset({"example.org", "example.edu"})))
    assert official.rules_hash(other) == official.rules_hash(same)
    assert official.rules_hash(other) != first


def test_recorded_auth_is_put_back() -> None:
    samples = dataset.load(SETS["synthetic"].samples)[:2]
    auth = {samples[0].id: "mx.example.com; dkim=pass header.d=example.edu"}
    found = official.with_recorded_auth(samples, auth)
    assert found[0].input.headers[AUTH_HEADER] == auth[samples[0].id]
    assert found[1] == samples[1]
    assert official.auth_of(found)[samples[0].id] == auth[samples[0].id]


# ---- Command line ----


def args(**changes: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "live": False,
        "recorded": False,
        "range": None,
        "replace_official": False,
        "save_replies": None,
        "save_link_replies": None,
        "live_links": False,
    }
    values.update(changes)
    return argparse.Namespace(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"live": True, "recorded": True},
        {"live": True, "save_replies": Path("x.json")},
        {"live": True, "save_link_replies": Path("x.json")},
        {"recorded": True, "live_links": True},
        {"recorded": True, "range": 2},
        {"recorded": True, "replace_official": True},
        {"live": True, "range": 2, "replace_official": True},
    ],
)
def test_bad_flag_mixes_are_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(SetError):
        official.check_args(args(**changes))


@pytest.mark.parametrize(
    "changes", [{"live": True}, {"recorded": True}, {"live": True, "range": 2}]
)
def test_good_flag_mixes_pass(changes: dict[str, Any]) -> None:
    official.check_args(args(**changes))


@pytest.mark.parametrize("flag", ["--recorded", "--replace-official", "--range=2"])
def test_test_only_flags_are_refused_for_other_sets(flag: str) -> None:
    with pytest.raises(SetError, match="--set test"):
        main(["--set", "synthetic", flag])


def test_test_live_is_refused_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")
    with pytest.raises(SetError, match="CI"):
        main(["--set", "test", "--live"])
