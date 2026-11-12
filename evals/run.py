"""Command line for the evaluation runner.

Recorded mode (the default, and the only mode CI uses) reads the replies file
next to the set: no API key, no network, no cost, the same numbers every run.

    uv run python -m evals.run --set synthetic

Live mode calls the model and is run by hand by the owner only. It reads
ANTHROPIC_API_KEY and SINAN_LLM_MODEL from the environment and refuses to
run in CI.

    uv run python -m evals.run --set synthetic --live --save-replies new.json

The report goes to stdout, or to --out. It holds summary metrics only.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings
from app.extraction.prefilter import EXAMPLE_RULES, Rules, load_rules
from app.extraction.prompt import PROMPT, prepare
from app.providers.anthropic_llm import AnthropicExtractor
from app.providers.llm import Extractor
from evals import dataset, replies
from evals.dataset import Sample, SetError
from evals.runner import Reply, report, run

HERE = Path(__file__).parent


@dataclass(frozen=True)
class SetFiles:
    samples: Path
    replies: Path


SETS = {
    "synthetic": SetFiles(HERE / "synthetic.jsonl", HERE / "synthetic.replies.json"),
}


class LiveReply:
    """Calls the model for each sample, keeping the raw replies for --save-replies."""

    def __init__(self, extractor: Extractor) -> None:
        self.extractor = extractor
        self.raw: dict[str, str] = {}

    def __call__(self, sample: Sample) -> str:
        snap = sample.input
        text = prepare(snap.subject, snap.from_addr, snap.sent_at, snap.body_text)
        raw = self.extractor.extract(PROMPT, text)
        self.raw[sample.id] = raw
        return raw


def live_extractor(env: dict[str, str]) -> AnthropicExtractor:
    """The real model. Refused in CI and without a key."""
    if env.get("CI", "").lower() == "true":
        raise SetError("live mode never runs in CI")
    # Environment variables only: Settings never reads a .env file
    settings = Settings()
    key = settings.anthropic_api_key
    if key is None:
        raise SetError("live mode needs ANTHROPIC_API_KEY")
    return AnthropicExtractor(key, settings.sinan_llm_model)


def evaluate(name: str, samples: Sequence[Sample], rules: Rules, reply: Reply) -> dict[str, Any]:
    outcomes = run(samples, rules, reply)
    model = "recorded"
    if isinstance(reply, LiveReply):
        model = reply.extractor.model
    mode = "recorded" if model == "recorded" else "live"
    return report(name, mode, model, outcomes)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="evals.run")
    parser.add_argument("--set", choices=sorted(SETS), default="synthetic")
    parser.add_argument("--rules", type=Path, default=EXAMPLE_RULES)
    parser.add_argument("--live", action="store_true", help="call the model (owner only)")
    parser.add_argument("--save-replies", type=Path, help="live mode: write the replies here")
    parser.add_argument("--out", type=Path, help="write the report here instead of stdout")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.save_replies and not args.live:
        raise SetError("--save-replies only works with --live")
    files = SETS[args.set]
    samples = dataset.load(files.samples)
    rules = load_rules(args.rules)
    reply: Reply
    if args.live:
        reply = LiveReply(live_extractor(dict(os.environ)))
    else:
        reply = replies.Recorded(replies.load(files.replies))
    result = evaluate(args.set, samples, rules, reply)
    text = json.dumps(result, indent=2) + "\n"
    if isinstance(reply, LiveReply) and args.save_replies:
        args.save_replies.write_text(replies.dump(reply.raw), encoding="utf-8")
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
