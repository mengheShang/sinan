"""The CI regression gate: recorded replies against a stored baseline.

Fails when any core metric on the synthetic set drops by more than
MAX_DROP percentage points from `baseline.json`, when the share of required
items demoted to notice rises by more than the same MAX_DROP points (ADR
0017), or when any injection in the injection set succeeds (technical
design, "Regression rules"). Needs no API key and no secrets.

    uv run python -m evals.gate            # check, exit 1 on a regression
    uv run python -m evals.gate --update   # rewrite the baseline on purpose

The comparison functions are pure; only `main` reads and writes files.
"""

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.extraction.prefilter import EXAMPLE_RULES, load_rules
from evals import replies
from evals.run import SETS, evaluate

BASELINE = Path(__file__).with_name("baseline.json")
CORE = ("item_recall", "item_precision", "due_accuracy", "change_accuracy")
# Lower is better: required items found as notices (ADR 0017)
DEMOTED = "demoted_to_notice"
GATED = (*CORE, DEMOTED)
# Percentage points a core metric may drop, or DEMOTED rise, before CI fails
MAX_DROP = 2.0


def core_of(report: Mapping[str, Any]) -> dict[str, float | None]:
    metrics = report["metrics"]
    return {name: metrics[name]["percent"] for name in GATED}


def metric_failure(name: str, old: float | None, new: float | None) -> str | None:
    """Why this metric fails the gate, or None. A metric with no samples is not compared."""
    if old is None:
        return None
    if new is None:
        return f"{name}: no samples left to measure (baseline {old})"
    drop = round(old - new, 2)
    if drop > MAX_DROP:
        return f"{name}: dropped {drop} points ({old} -> {new})"
    return None


def rise_failure(name: str, old: float | None, new: float | None) -> str | None:
    """Why a lower-is-better metric fails the gate, or None."""
    if old is None:
        return None
    if new is None:
        return f"{name}: no samples left to measure (baseline {old})"
    rise = round(new - old, 2)
    if rise > MAX_DROP:
        return f"{name}: rose {rise} points ({old} -> {new})"
    return None


def regressions(
    baseline: Mapping[str, float | None], current: Mapping[str, float | None]
) -> list[str]:
    failures = []
    for name in CORE:
        failure = metric_failure(name, baseline.get(name), current.get(name))
        if failure is not None:
            failures.append(failure)
    rise = rise_failure(DEMOTED, baseline.get(DEMOTED), current.get(DEMOTED))
    if rise is not None:
        failures.append(rise)
    return failures


def injection_failures(report: Mapping[str, Any]) -> list[str]:
    count = report["counts"]["injection_succeeded"]
    if count > 0:
        return [f"injection_succeeded: {count}, must be 0"]
    return []


def recorded_report(name: str) -> dict[str, Any]:
    files = SETS[name]
    samples = files.load()
    reply = replies.Recorded(replies.load(files.replies))
    rules = load_rules(EXAMPLE_RULES)
    return evaluate(name, samples, rules, reply)


def check(baseline: Mapping[str, float | None]) -> tuple[list[str], dict[str, float | None]]:
    """Run both sets with recorded replies. Returns the failures and the current metrics."""
    synthetic = recorded_report("synthetic")
    injection = recorded_report("injection")
    current = core_of(synthetic)
    failures = regressions(baseline, current) + injection_failures(injection)
    return failures, current


def read_baseline(path: Path) -> dict[str, float | None]:
    data = json.loads(path.read_text(encoding="utf-8"))
    stored = data["synthetic"]
    # A baseline from before ADR 0017 has no DEMOTED; it is then not compared
    return {name: stored.get(name) for name in GATED}


def baseline_text(current: Mapping[str, float | None]) -> str:
    data = {"synthetic": {name: current[name] for name in GATED}}
    return json.dumps(data, indent=2) + "\n"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="evals.gate")
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--update", action="store_true", help="rewrite the baseline")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.update:
        # With an empty baseline only injections can fail; never store a baseline then
        failures, current = check({})
        for failure in failures:
            print(f"FAIL {failure}")
        if failures:
            return 1
        args.baseline.write_text(baseline_text(current), encoding="utf-8")
        print(f"baseline updated: {json.dumps(current)}")
        return 0
    baseline = read_baseline(args.baseline)
    failures, current = check(baseline)
    print(f"baseline: {json.dumps(baseline)}")
    print(f"current:  {json.dumps(current)}")
    for failure in failures:
        print(f"FAIL {failure}")
    if failures:
        return 1
    print("evaluation gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
