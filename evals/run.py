"""Command line for the evaluation runner.

Recorded mode (the default, and the only mode CI uses) reads the replies file
next to the set: no API key, no network, no cost, the same numbers every run.

    uv run python -m evals.run --set synthetic
    uv run python -m evals.run --set injection

Live mode calls the model and is run by hand by the owner only. It reads
ANTHROPIC_API_KEY and SINAN_LLM_MODEL from the environment and refuses to
run in CI.

    uv run python -m evals.run --set synthetic --live --save-replies new.json

The link step (ADR 0019) has its own recorded replies next to the set. Live
mode asks the model for those too; `--live-links` asks only the link step
live, over the recorded extraction replies, and `--save-link-replies` writes
what it said. The owner records them in a Worker container, which has the
key:

    docker compose run --rm --no-deps -v ./evals:/srv/evals worker \
        python -m evals.run --set synthetic --live-links \
        --save-link-replies evals/synthetic.links.json

The real dev set (`--set dev`) is read from the label table and is live only:
its replies come from real mail and are never recorded. The owner runs it in
a Worker container, which has the database, the key and the local rules:

    docker compose run --rm --no-deps -v ./evals:/srv/evals:ro worker \
        python -m evals.run --set dev --live

Its report adds `set_version` and `parts`: the same counts and metrics for
the random dev draw and for the `deadline_boost` supplement alone (Issue #78,
ADR 0018). Its snapshots predate sender trust (ADR 0010), so the run reads
each email's `Authentication-Results` again from Gmail with the Worker's
read-only token, in memory only, and trusts the school accounts' domains as
the Worker does. `sender_trust.auth_fetched` and `auth_missing` count how
that went.

`--model` picks the model for one live run instead of SINAN_LLM_MODEL, so
candidates can be compared (Issue #43). A live run's report adds `usage`:
calls, summed input and output tokens, and median and p90 latency per call,
for the extraction and the link step apart. Counts and timings only.

The report goes to stdout, or to --out. It holds summary metrics only.
"""

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.extract import school_addresses
from app.db.session import get_engine
from app.extraction.link_prompt import LINK_PROMPT_VERSION
from app.extraction.prefilter import EXAMPLE_RULES, Rules, load_rules
from app.extraction.prompt import PROMPT, prepare
from app.extraction.trust import with_school
from app.providers.anthropic_llm import AnthropicExtractor, AnthropicLinkPicker, client_messages
from app.providers.llm import Extractor, LinkPicker
from evals import dataset, real, replies
from evals.dataset import Sample, SetError
from evals.injection import InjectionCase, count_succeeded, load_cases
from evals.runner import Links, Reply, link_all, report, run
from evals.usage import Metered, usage_data

HERE = Path(__file__).parent

# Read from the label table, never from a file
DEV = "dev"


@dataclass(frozen=True)
class SetFiles:
    samples: Path
    replies: Path
    # The link step's recorded replies (ADR 0019)
    links: Path
    # Injection cases carry an expected outcome and add a success count
    injection: bool = False

    def load(self) -> list[Sample]:
        if self.injection:
            return list(load_cases(self.samples))
        return dataset.load(self.samples)


SETS = {
    "synthetic": SetFiles(
        HERE / "synthetic.jsonl", HERE / "synthetic.replies.json", HERE / "synthetic.links.json"
    ),
    "injection": SetFiles(
        HERE / "injection.jsonl",
        HERE / "injection.replies.json",
        HERE / "injection.links.json",
        True,
    ),
}


class LiveReply:
    """Calls the model for each sample, keeping the raw replies for --save-replies."""

    def __init__(self, extractor: Extractor, meter: Metered | None = None) -> None:
        self.extractor = extractor
        self.meter = meter
        self.raw: dict[str, str] = {}

    def __call__(self, sample: Sample) -> str:
        snap = sample.input
        text = prepare(snap.subject, snap.from_addr, snap.sent_at, snap.body_text)
        raw = self.extractor.extract(PROMPT, text)
        self.raw[sample.id] = raw
        return raw


class LiveLinks:
    """Asks the link step live, keeping the raw replies for --save-link-replies."""

    def __init__(self, picker: LinkPicker, meter: Metered | None = None) -> None:
        self.picker = picker
        self.meter = meter
        self.raw: dict[str, str] = {}

    def __call__(self, uid: str, prompt: str, text: str) -> str:
        raw = self.picker.pick(prompt, text)
        self.raw[uid] = raw
        return raw


def live_key(env: dict[str, str], model: str | None = None) -> tuple[SecretStr, str]:
    """The API key and model for a live run. Refused in CI and without a key.

    `model` overrides SINAN_LLM_MODEL for this run only.
    """
    if env.get("CI", "").lower() == "true":
        raise SetError("live mode never runs in CI")
    # Environment variables only: Settings never reads a .env file
    settings = Settings()
    key = settings.anthropic_api_key
    if key is None:
        raise SetError("live mode needs ANTHROPIC_API_KEY")
    return key, model or settings.sinan_llm_model


def live_extractor(env: dict[str, str], model: str | None = None) -> AnthropicExtractor:
    """The real model. Refused in CI and without a key."""
    key, name = live_key(env, model)
    return AnthropicExtractor(key, name)


def live_picker(env: dict[str, str], model: str | None = None) -> AnthropicLinkPicker:
    """The real link step, on the extraction model. Refused in CI and without a key."""
    key, name = live_key(env, model)
    return AnthropicLinkPicker(key, name)


def live_reply(env: dict[str, str], model: str | None = None) -> LiveReply:
    """Live extraction with its calls metered."""
    key, name = live_key(env, model)
    meter = Metered(client_messages(key))
    return LiveReply(AnthropicExtractor(key, name, meter), meter)


def live_links(env: dict[str, str], model: str | None = None) -> LiveLinks:
    """The live link step with its calls metered."""
    key, name = live_key(env, model)
    meter = Metered(client_messages(key))
    return LiveLinks(AnthropicLinkPicker(key, name, meter), meter)


def usage_of(reply: Reply, links: Links | None) -> dict[str, Any] | None:
    """Tokens and latency of the metered live calls, or None in recorded mode."""
    extraction = reply.meter if isinstance(reply, LiveReply) else None
    linking = links.meter if isinstance(links, LiveLinks) else None
    if extraction is None and linking is None:
        return None
    return {
        "extraction": usage_data(extraction) if extraction else None,
        "linking": usage_data(linking) if linking else None,
    }


def evaluate(
    name: str,
    samples: Sequence[Sample],
    rules: Rules,
    reply: Reply,
    parts: Mapping[str, str] | None = None,
    links: Links | None = None,
) -> dict[str, Any]:
    outcomes = run(samples, rules, reply)
    model = "recorded"
    if isinstance(reply, LiveReply):
        model = reply.extractor.model
    mode = "recorded" if model == "recorded" else "live"
    linked = link_all(outcomes, links)
    result = report(name, mode, model, outcomes, parts, linked=linked)
    result["linking"]["mode"] = "live" if isinstance(links, LiveLinks) else "recorded"
    if samples and all(isinstance(s, InjectionCase) for s in samples):
        counts = result["counts"]
        counts["injection_succeeded"] = count_succeeded(outcomes, linked.emails)
    usage = usage_of(reply, links)
    if usage is not None:
        result["usage"] = usage
    return result


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="evals.run")
    parser.add_argument("--set", choices=[*sorted(SETS), DEV], default="synthetic")
    parser.add_argument(
        "--rules",
        type=Path,
        help="prefilter rules; the example rules, or for dev the Worker's rules",
    )
    parser.add_argument("--live", action="store_true", help="call the model (owner only)")
    parser.add_argument("--save-replies", type=Path, help="live mode: write the replies here")
    parser.add_argument(
        "--live-links",
        action="store_true",
        help="ask only the link step live, over recorded replies (owner only)",
    )
    parser.add_argument(
        "--save-link-replies", type=Path, help="live link step: write its replies here"
    )
    parser.add_argument(
        "--model", help="live mode: the model for this run instead of SINAN_LLM_MODEL"
    )
    parser.add_argument("--out", type=Path, help="write the report here instead of stdout")
    return parser.parse_args(argv)


def worker_rules(settings: Settings) -> Path:
    """The rules the Worker loads: the local file, else the example rules."""
    local = Path(settings.sinan_prefilter_rules)
    return local if local.is_file() else EXAMPLE_RULES


def evaluate_dev(args: argparse.Namespace) -> dict[str, Any]:
    """The real dev set from the label table, live only, split into parts."""
    if not args.live:
        raise SetError("the dev set has no recorded replies; use --live")
    if args.save_replies:
        raise SetError("replies of real mail are never saved")
    if args.save_link_replies:
        raise SetError("replies of real mail are never saved")
    # Checked before reading any mail
    env = dict(os.environ)
    reply = live_reply(env, args.model)
    links = live_links(env, args.model)
    settings = Settings()
    rules = load_rules(args.rules or worker_rules(settings))
    # Imported here: the Worker's readers need the live OAuth settings
    from apps.worker.sync_job import build_readers

    with Session(get_engine()) as session:
        found = real.load_dev(session)
        rules = with_school(rules, school_addresses(session))
        fetched = real.with_auth(session, found, build_readers(settings))
        # A token refresh may flag an account; the Worker's sync records that
        session.rollback()
    result = evaluate(DEV, fetched.samples, rules, reply, found.parts, links)
    result["set_version"] = found.set_version
    result["sender_trust"]["auth_fetched"] = fetched.fetched
    result["sender_trust"]["auth_missing"] = fetched.missing
    return result


def evaluate_file(args: argparse.Namespace) -> dict[str, Any]:
    files = SETS[args.set]
    samples = files.load()
    rules = load_rules(args.rules or EXAMPLE_RULES)
    reply: Reply
    if args.live:
        reply = live_reply(dict(os.environ), args.model)
    else:
        reply = replies.Recorded(replies.load(files.replies))
    links: Links
    if args.live or args.live_links:
        links = live_links(dict(os.environ), args.model)
    else:
        links = replies.RecordedLinks(replies.load_links(files.links))
    result = evaluate(args.set, samples, rules, reply, links=links)
    if isinstance(reply, LiveReply) and args.save_replies:
        args.save_replies.write_text(replies.dump(reply.raw), encoding="utf-8")
    if isinstance(links, LiveLinks) and args.save_link_replies:
        text = replies.dump(links.raw, LINK_PROMPT_VERSION)
        args.save_link_replies.write_text(text, encoding="utf-8")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.save_replies and not args.live:
        raise SetError("--save-replies only works with --live")
    if args.save_link_replies and not (args.live or args.live_links):
        raise SetError("--save-link-replies only works with --live or --live-links")
    if args.model and not (args.live or args.live_links):
        raise SetError("--model only works with --live or --live-links")
    result = evaluate_dev(args) if args.set == DEV else evaluate_file(args)
    text = json.dumps(result, indent=2) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
