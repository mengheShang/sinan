"""Live-run model choice and the token and latency counters (Issue #43)."""

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from app.extraction.prefilter import EXAMPLE_RULES, load_rules
from app.providers.anthropic_llm import AnthropicExtractor, AnthropicLinkPicker
from evals import dataset, replies
from evals.dataset import SetError
from evals.run import SETS, LiveLinks, LiveReply, evaluate, live_key, main
from evals.usage import Metered, p90, usage_data

KEY = SecretStr("sk-test-not-a-real-key")
FAKE_KEY = "sk-test-not-a-real-key"


class FakeMessages:
    """Answers every request with an empty item list and fixed token counts."""

    def __init__(self, reply: str = '{"items": []}') -> None:
        self.reply = reply
        self.models: list[str] = []

    def create(self, **kwargs: Any) -> Any:
        self.models.append(kwargs["model"])
        block = SimpleNamespace(type="text", text=self.reply)
        usage = SimpleNamespace(input_tokens=100, output_tokens=7)
        return SimpleNamespace(stop_reason="end_turn", content=[block], usage=usage)


def ticks(step: float) -> Iterator[float]:
    now = 0.0
    while True:
        yield now
        now += step


def test_model_override_replaces_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setenv("SINAN_LLM_MODEL", "setting-model")
    assert live_key({})[1] == "setting-model"
    assert live_key({}, "other-model")[1] == "other-model"


def test_model_override_still_refuses_ci() -> None:
    with pytest.raises(SetError, match="never runs in CI"):
        live_key({"CI": "true"}, "other-model")


def test_model_needs_live_mode() -> None:
    with pytest.raises(SetError, match="--model only works"):
        main(["--set", "synthetic", "--model", "other-model"])


def test_metered_sums_tokens_and_times_each_call() -> None:
    clock = ticks(0.5)
    meter = Metered(FakeMessages(), lambda: next(clock))
    meter.create(model="m")
    meter.create(model="m")
    assert meter.input_tokens == 200
    assert meter.output_tokens == 14
    assert meter.seconds == [0.5, 0.5]


def test_a_failed_call_is_not_counted() -> None:
    class Failing:
        def create(self, **kwargs: Any) -> Any:
            raise RuntimeError("down")

    meter = Metered(Failing())
    with pytest.raises(RuntimeError):
        meter.create(model="m")
    assert meter.seconds == []
    assert usage_data(meter)["latency_ms"] is None


def test_p90_is_nearest_rank() -> None:
    values = [float(n) for n in range(1, 11)]
    assert p90(values) == 9.0
    assert p90([3.0]) == 3.0


def test_usage_data_is_in_milliseconds() -> None:
    meter = Metered(FakeMessages())
    meter.seconds = [0.1, 0.2, 0.3, 0.4, 2.0]
    data = usage_data(meter)
    assert data["calls"] == 5
    assert data["latency_ms"] == {"median": 300, "p90": 2000}


def metered_report(model: str) -> tuple[dict[str, Any], FakeMessages]:
    files = SETS["synthetic"]
    samples = dataset.load(files.samples)
    rules = load_rules(EXAMPLE_RULES)
    fake = FakeMessages()
    meter = Metered(fake)
    reply = LiveReply(AnthropicExtractor(KEY, model, meter), meter)
    link_meter = Metered(fake)
    links = LiveLinks(AnthropicLinkPicker(KEY, model, link_meter), link_meter)
    return evaluate("synthetic", samples, rules, reply, links=links), fake


def strings(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from strings(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from strings(inner)
    elif isinstance(value, str):
        yield value


def test_live_report_names_the_model_and_adds_usage() -> None:
    result, fake = metered_report("other-model")
    assert result["model"] == "other-model"
    assert set(fake.models) == {"other-model"}
    usage = result["usage"]
    extraction = usage["extraction"]
    assert extraction["calls"] > 0
    assert extraction["input_tokens"] == 100 * extraction["calls"]
    assert extraction["output_tokens"] == 7 * extraction["calls"]
    # Empty replies hold no changes, so the link step is never asked
    assert usage["linking"]["calls"] == 0


def test_usage_holds_fixed_keys_and_numbers_only() -> None:
    result, _ = metered_report("other-model")
    usage = result["usage"]
    allowed = {
        "extraction",
        "linking",
        "calls",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "median",
        "p90",
    }
    assert set(strings(usage)) <= allowed
    for part in usage.values():
        assert set(part) == {"calls", "input_tokens", "output_tokens", "latency_ms"}
    assert "sk-test" not in json.dumps(result)


def test_recorded_report_has_no_usage() -> None:
    files = SETS["synthetic"]

    samples = dataset.load(files.samples)
    reply = replies.Recorded(replies.load(files.replies))
    result = evaluate("synthetic", samples, load_rules(EXAMPLE_RULES), reply)
    assert "usage" not in result
