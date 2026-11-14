"""The evaluation runner: recorded mode, the CLI and summary-only reports."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.extraction.prefilter import EXAMPLE_RULES, Rules, load_rules
from app.extraction.prompt import PROMPT_VERSION
from app.extraction.validate import check
from evals import dataset, replies
from evals.dataset import Sample, SetError
from evals.metrics import Found
from evals.run import SETS, LiveReply, evaluate, live_extractor, main
from evals.runner import Linker, Outcome, found_of, report, run

SAMPLE = {
    "id": "t-001",
    "input": {
        "from_addr": "rivera@example.edu",
        "subject": "CS 000: Homework 3 posted",
        "body_text": "Homework 3 is due Friday Nov 13 at 11:59pm PT.",
        "sent_at": "2026-11-02T17:00:00Z",
        "role": "school",
        "headers": {},
    },
    "labels": {
        "items": [
            {
                "ref": "t-hw3",
                "kind": "deadline",
                "course": "CS 000",
                "title": "Homework 3",
                "due_raw": "Friday Nov 13 at 11:59pm PT",
                "due_at": "2026-11-14T07:59:00Z",
                "due_ambiguous": False,
                "changes": None,
            }
        ],
        "safe_to_auto_read": False,
    },
}

GOOD_REPLY = {
    "items": [
        {
            "kind": "deadline",
            "course": "CS 000",
            "title": "Homework 3",
            "evidence": "Homework 3 is due Friday Nov 13 at 11:59pm PT.",
            "due_raw": "Friday Nov 13 at 11:59pm PT",
            "change_type": "create",
        }
    ]
}


def one_sample(**changes: Any) -> Sample:
    data = json.loads(json.dumps(SAMPLE))
    data["input"].update(changes)
    return Sample.model_validate_json(json.dumps(data))


def rules() -> Rules:
    return load_rules(EXAMPLE_RULES)


def synthetic_report() -> dict[str, Any]:
    files = SETS["synthetic"]
    samples = dataset.load(files.samples)
    reply = replies.Recorded(replies.load(files.replies))
    return evaluate("synthetic", samples, rules(), reply)


def strings(value: Any) -> Iterator[str]:
    """Every key and string value in a JSON-like structure."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from strings(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from strings(inner)
    elif isinstance(value, str):
        yield value


def test_recorded_run_is_the_same_every_time() -> None:
    first = synthetic_report()
    second = synthetic_report()
    assert first == second
    assert first["counts"]["emails"] == 58
    assert first["mode"] == "recorded"


def test_recorded_replies_cover_every_sample() -> None:
    files = SETS["synthetic"]
    ids = {s.id for s in dataset.load(files.samples)}
    assert set(replies.load(files.replies)) == ids


def test_report_holds_no_email_content() -> None:
    result = synthetic_report()
    text = json.dumps(result)
    for sample in dataset.load(SETS["synthetic"].samples):
        snap = sample.input
        assert snap.subject not in text
        assert snap.body_text not in text
        assert snap.from_addr not in text
        assert sample.id not in text
        for item in sample.labels.items:
            assert item.title not in text
            assert item.ref not in text


def test_report_holds_fixed_names_only() -> None:
    """Every key and string in the report is a fixed name, so nothing can leak."""
    allowed = {
        "set",
        "mode",
        "model",
        "prompt_version",
        "counts",
        "metrics",
        "emails",
        "prefilter_skipped",
        "invalid_replies",
        "labeled_items",
        "found_items",
        "matched_items",
        "item_recall",
        "item_precision",
        "due_accuracy",
        "change_accuracy",
        "demoted_to_notice",
        "percent",
        "hits",
        "total",
        "synthetic",
        "recorded",
        PROMPT_VERSION,
    }
    result = synthetic_report()
    assert set(strings(result)) <= allowed


def test_report_of_one_good_reply() -> None:
    sample = one_sample()
    outcomes = run([sample], rules(), lambda s: json.dumps(GOOD_REPLY))
    result = report("t", "recorded", "recorded", outcomes)
    recall = result["metrics"]["item_recall"]
    assert recall == {"percent": 100.0, "hits": 1, "total": 1}
    assert result["metrics"]["due_accuracy"]["percent"] == 100.0
    assert result["metrics"]["change_accuracy"]["percent"] is None


def test_prefilter_skip_finds_nothing_and_asks_no_model() -> None:
    sample = one_sample(headers={"Precedence": "bulk"})
    calls: list[str] = []

    def reply(s: Sample) -> str:
        calls.append(s.id)
        return json.dumps(GOOD_REPLY)

    outcomes = run([sample], rules(), reply)
    result = report("t", "recorded", "recorded", outcomes)
    assert calls == []
    assert result["counts"]["prefilter_skipped"] == 1
    assert result["metrics"]["item_recall"]["hits"] == 0


def test_invalid_reply_is_counted() -> None:
    outcomes = run([one_sample()], rules(), lambda s: "not json")
    result = report("t", "recorded", "recorded", outcomes)
    assert result["counts"]["invalid_replies"] == 1
    assert result["counts"]["found_items"] == 0


def test_missing_recorded_reply_fails() -> None:
    reply = replies.Recorded({})
    with pytest.raises(SetError, match="no recorded reply"):
        reply(one_sample())


def test_replies_for_another_prompt_are_refused() -> None:
    text = json.dumps({"prompt_version": "v0", "replies": {}})
    with pytest.raises(SetError, match="recorded for prompt v0"):
        replies.parse(text)


def test_malformed_replies_file_is_refused() -> None:
    with pytest.raises(SetError, match="malformed"):
        replies.parse('{"replies": []}')


def test_string_reply_is_used_as_raw_text() -> None:
    text = json.dumps({"prompt_version": PROMPT_VERSION, "replies": {"a": "{oops", "b": {}}})
    assert replies.parse(text) == {"a": "{oops", "b": "{}"}


def test_dump_round_trips() -> None:
    raw = {"b": json.dumps(GOOD_REPLY), "a": "{oops"}
    assert replies.parse(replies.dump(raw)) == {"a": "{oops", "b": json.dumps(GOOD_REPLY)}


def test_live_mode_is_refused_in_ci() -> None:
    with pytest.raises(SetError, match="never runs in CI"):
        live_extractor({"CI": "true"})


def test_live_mode_needs_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SetError, match="needs ANTHROPIC_API_KEY"):
        live_extractor({})


class Canned:
    model = "canned-model"

    def __init__(self) -> None:
        self.texts: list[str] = []

    def extract(self, prompt: str, text: str) -> str:
        self.texts.append(text)
        return json.dumps(GOOD_REPLY)


def test_live_reply_sends_the_prepared_text_and_keeps_replies() -> None:
    model = Canned()
    reply = LiveReply(model)
    result = evaluate("t", [one_sample()], rules(), reply)
    assert result["mode"] == "live"
    assert result["model"] == "canned-model"
    assert model.texts[0].startswith("<email>")
    assert reply.raw == {"t-001": json.dumps(GOOD_REPLY)}


def test_cli_writes_the_report(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    assert main(["--set", "synthetic", "--out", str(out)]) == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written == synthetic_report()


def test_cli_refuses_save_replies_without_live(tmp_path: Path) -> None:
    with pytest.raises(SetError, match="only works with --live"):
        main(["--save-replies", str(tmp_path / "r.json")])


def test_changes_link_through_a_chain() -> None:
    """A change links to the item found for its target; the next change to it."""
    linker = Linker()
    first = found_one("a", "create", "Friday Nov 13 at 11:59pm PT", linker)
    second = found_one("b", "update", "Monday Nov 16 at 11:59pm PT", linker)
    third = found_one("c", "update", "Monday Nov 30 at 11:59pm PT", linker)
    assert first.target is None
    assert second.target == first.uid
    assert third.target == second.uid


def test_change_in_the_same_email_is_not_linked() -> None:
    linker = Linker()
    found_one("a", "create", "Friday Nov 13 at 11:59pm PT", linker)
    assert found_one("a", "update", "Monday Nov 16 at 11:59pm PT", linker).target is None


def test_unparsed_change_is_not_linked() -> None:
    linker = Linker()
    found_one("a", "create", "Friday Nov 13 at 11:59pm PT", linker)
    assert found_one("b", "update", "11/30", linker).target is None


def test_synthetic_changes_score() -> None:
    change = synthetic_report()["metrics"]["change_accuracy"]
    assert change["hits"] == change["total"] > 0


def found_one(sample_id: str, change: str, due_raw: str, linker: Linker) -> Found:
    evidence = f"Homework 3 is due {due_raw}."
    item = {
        "kind": "deadline",
        "course": "CS 000",
        "title": "Homework 3",
        "evidence": evidence,
        "due_raw": due_raw,
        "change_type": change,
    }
    sent = datetime(2026, 11, 2, 17, 0, tzinfo=UTC)
    checked = check(json.dumps({"items": [item]}), evidence, sent)
    sample = one_sample().model_copy(update={"id": sample_id})
    (found,) = found_of(Outcome(sample, False, checked), linker)
    return found
