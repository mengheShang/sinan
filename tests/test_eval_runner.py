"""The evaluation runner: recorded mode, the CLI and summary-only reports."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.extraction.link_prompt import LINK_PROMPT_VERSION
from app.extraction.prefilter import EXAMPLE_RULES, Rules, decide, load_rules
from app.extraction.prompt import PROMPT_VERSION
from app.extraction.trust import REASONS as TRUST_REASONS
from app.extraction.validate import check
from evals import dataset, replies
from evals.dataset import Sample, SetError
from evals.metrics import Found
from evals.run import SETS, LiveReply, evaluate, live_extractor, live_picker, main
from evals.runner import Linker, Outcome, envelope, found_of, report, run, to_emails

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
    links = replies.RecordedLinks(replies.load_links(files.links))
    return evaluate("synthetic", samples, rules(), reply, links=links)


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
    assert first["counts"]["emails"] == 68
    assert first["mode"] == "recorded"


def test_recorded_replies_cover_every_sample_the_prefilter_passes() -> None:
    """A live recording saves replies only for mail that reached the model."""
    files = SETS["synthetic"]
    rules = load_rules(EXAMPLE_RULES)
    samples = dataset.load(files.samples)
    passed = {s.id for s in samples if decide(envelope(s), rules).action == "pass"}
    known = {s.id for s in samples}
    recorded = set(replies.load(files.replies))
    assert passed <= recorded
    assert recorded <= known


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
        "notice_escalated",
        "email_groups",
        "none_to_none",
        "none_to_notice_only",
        "none_to_required",
        "notice_only_to_none",
        "notice_only_to_notice_only",
        "notice_only_to_required",
        "required_to_none",
        "required_to_notice_only",
        "required_to_required",
        "percent",
        "hits",
        "total",
        "synthetic",
        "recorded",
        PROMPT_VERSION,
        "linking",
        "link_prompt_version",
        "change_accuracy_without_llm",
        "change_accuracy_with_llm",
        "spurious_links_without_llm",
        "spurious_links_with_llm",
        "asked",
        "picked",
        "refused",
        "failed",
        LINK_PROMPT_VERSION,
        "sender_trust",
        "email_reasons",
        "found_items_held",
        "roles",
        "school",
        "personal",
        "required_items",
        "held",
        *TRUST_REASONS,
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


def test_synthetic_changes_are_scored_end_to_end() -> None:
    """Changes are linked and scored; how many is the gate's job, not this test's."""
    change = synthetic_report()["metrics"]["change_accuracy"]
    assert change["total"] > 0
    assert change["hits"] > 0


class AskedLinks:
    """Link replies by uid, remembering which changes were asked."""

    def __init__(self, raw: str = '{"target": 1}') -> None:
        self.raw = raw
        self.asked: list[str] = []

    def __call__(self, uid: str, prompt: str, text: str) -> str:
        self.asked.append(uid)
        return self.raw


def test_one_deterministic_candidate_skips_the_link_step() -> None:
    links = AskedLinks()
    linker = Linker(links)
    first = found_one("a", "create", "Friday Nov 13 at 11:59pm PT", linker)
    second = found_one("b", "update", "Monday Nov 16 at 11:59pm PT", linker)
    assert second.target == first.uid
    assert links.asked == []


def test_no_deterministic_candidate_asks_the_link_step() -> None:
    links = AskedLinks()
    linker = Linker(links)
    first = found_one("a", "create", "Friday Nov 13 at 11:59pm PT", linker)
    title = "Third homework"
    second = found_one("b", "update", "Monday Nov 16 at 11:59pm PT", linker, title)
    assert links.asked == ["b/0"]
    assert second.target == first.uid
    assert linker.counts.picked == 1


def test_several_deterministic_candidates_ask_the_link_step() -> None:
    links = AskedLinks('{"target": 2}')
    linker = Linker(links)
    found_one("a", "create", "Friday Nov 13 at 11:59pm PT", linker, "Homework 3 draft")
    final = found_one("b", "create", "Friday Nov 20 at 11:59pm PT", linker, "Homework 3 final")
    change = found_one("c", "update", "Monday Nov 23 at 11:59pm PT", linker)
    assert links.asked == ["c/0"]
    # Both emails have the same date, so the choices keep their order
    assert change.target == final.uid


@pytest.mark.parametrize("raw", ['{"target": 5}', '{"target": 1, "apply": true}', "oops"])
def test_a_refused_link_reply_leaves_the_change_unlinked(raw: str) -> None:
    linker = Linker(AskedLinks(raw))
    found_one("a", "create", "Friday Nov 13 at 11:59pm PT", linker)
    second = found_one("b", "update", "Monday Nov 16 at 11:59pm PT", linker, "Third homework")
    assert second.target is None
    assert linker.counts.refused == 1


def test_without_link_replies_nothing_is_asked() -> None:
    linker = Linker()
    found_one("a", "create", "Friday Nov 13 at 11:59pm PT", linker)
    second = found_one("b", "update", "Monday Nov 16 at 11:59pm PT", linker, "Third homework")
    assert second.target is None


def test_recorded_link_replies_cover_exactly_the_asked_changes() -> None:
    for name in ("synthetic", "injection"):
        files = SETS[name]
        samples = files.load()
        outcomes = run(samples, rules(), replies.Recorded(replies.load(files.replies)))
        links = AskedLinks('{"target": null}')
        to_emails(outcomes, Linker(links))
        assert set(links.asked) == set(replies.load_links(files.links))


def test_a_missing_link_reply_fails_loudly() -> None:
    recorded = replies.RecordedLinks({})
    with pytest.raises(SetError, match="no recorded link reply"):
        recorded("syn-001/0", "prompt", "text")


def test_link_step_raises_synthetic_change_accuracy() -> None:
    linking = synthetic_report()["linking"]
    without = linking["change_accuracy_without_llm"]
    with_llm = linking["change_accuracy_with_llm"]
    assert with_llm["hits"] > without["hits"]
    assert linking["spurious_links_with_llm"] == 0
    assert linking["asked"] == linking["picked"] + 1


def test_report_without_link_replies_says_so() -> None:
    sample = one_sample()
    outcomes = run([sample], rules(), lambda s: json.dumps(GOOD_REPLY))
    linking = report("t", "recorded", "recorded", outcomes)["linking"]
    assert linking["change_accuracy_with_llm"] is None
    assert linking["link_prompt_version"] is None


def test_cli_refuses_save_link_replies_without_live(tmp_path: Path) -> None:
    with pytest.raises(SetError, match="only works with --live or --live-links"):
        main(["--save-link-replies", str(tmp_path / "l.json")])


def test_live_link_step_never_runs_in_ci() -> None:
    with pytest.raises(SetError, match="never runs in CI"):
        live_picker({"CI": "true"})


def found_one(
    sample_id: str, change: str, due_raw: str, linker: Linker, title: str = "Homework 3"
) -> Found:
    evidence = f"{title} is due {due_raw}."
    item = {
        "kind": "deadline",
        "course": "CS 000",
        "title": title,
        "evidence": evidence,
        "due_raw": due_raw,
        "change_type": change,
    }
    sent = datetime(2026, 11, 2, 17, 0, tzinfo=UTC)
    checked = check(json.dumps({"items": [item]}), evidence, sent)
    sample = one_sample().model_copy(update={"id": sample_id})
    (found,) = found_of(Outcome(sample, False, checked), linker)
    return found


# ---- Parts of a set (Issue #78) ----


def split_report() -> tuple[dict[str, Any], dict[str, str]]:
    """The synthetic set split in two parts, as the dev set and its supplement."""
    files = SETS["synthetic"]
    samples = dataset.load(files.samples)
    reply = replies.Recorded(replies.load(files.replies))
    names = ("random", "deadline_boost")
    ids = sorted(s.id for s in samples)
    parts = {sid: names[n % 2] for n, sid in enumerate(ids)}
    links = replies.RecordedLinks(replies.load_links(files.links))
    return evaluate("synthetic", samples, rules(), reply, parts, links), parts


def test_report_splits_the_parts_and_they_add_up() -> None:
    result, parts = split_report()
    whole = synthetic_report()
    assert result["counts"] == whole["counts"]
    assert result["metrics"] == whole["metrics"]
    boost = result["parts"]["deadline_boost"]
    rest = result["parts"]["random"]
    assert set(result["parts"]) == {"deadline_boost", "random"}
    assert boost["counts"]["emails"] == sum(1 for p in parts.values() if p == "deadline_boost")
    for key in ("emails", "labeled_items", "found_items", "matched_items", "prefilter_skipped"):
        assert boost["counts"][key] + rest["counts"][key] == whole["counts"][key]
    for name in ("item_recall", "item_precision", "due_accuracy", "change_accuracy"):
        hits = boost["metrics"][name]["hits"] + rest["metrics"][name]["hits"]
        total = boost["metrics"][name]["total"] + rest["metrics"][name]["total"]
        # A change in one part still links to its target in the other
        assert (hits, total) == (whole["metrics"][name]["hits"], whole["metrics"][name]["total"])


def test_split_report_holds_fixed_names_only() -> None:
    result, _ = split_report()
    text = json.dumps(result)
    for sample in dataset.load(SETS["synthetic"].samples):
        assert sample.id not in text
        assert sample.input.subject not in text
    whole = set(strings(synthetic_report()))
    names = whole | {"parts", "random", "deadline_boost"}
    assert set(strings(result)) <= names


def test_report_without_parts_has_no_parts_key() -> None:
    assert "parts" not in synthetic_report()


def test_dev_set_is_live_only(tmp_path: Path) -> None:
    with pytest.raises(SetError, match="use --live"):
        main(["--set", "dev"])
    with pytest.raises(SetError, match="never saved"):
        main(["--set", "dev", "--live", "--save-replies", str(tmp_path / "r.json")])
