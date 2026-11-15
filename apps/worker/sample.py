"""Command line for drawing the real development and test sets (Issue #41).

The owner runs it in the Worker container, which has the database and the
local prefilter rules:

    docker compose exec worker python -m apps.worker.sample --seed 20261112
    docker compose exec worker python -m apps.worker.sample --seed 20261112 --write

Without --write it only shows what it would draw. With --write it records the
draw. A second run is refused: the test set is fixed once drawn. A new dev set
is drawn with --redraw-dev.

--boost-dev 40 adds a supplement of 40 emails likely to hold a deadline to the
current dev set version (Issue #78, ADR 0018), once per version:

    docker compose exec worker python -m apps.worker.sample --seed 20261114 --boost-dev 40

It prints counts only, never an address, a subject or a body.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import boost, sampling
from app.db.session import get_engine
from app.extraction.prefilter import EXAMPLE_RULES, load_rules
from apps.worker.extract_job import rules_path


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="apps.worker.sample")
    parser.add_argument("--seed", type=int, required=True, help="any fixed integer")
    parser.add_argument("--write", action="store_true", help="record the draw")
    parser.add_argument(
        "--redraw-dev",
        action="store_true",
        help="draw a new dev set with the next set version; the test set stays",
    )
    parser.add_argument(
        "--boost-dev",
        type=int,
        metavar="N",
        help="add N emails likely to hold a deadline to the current dev set",
    )
    args = parser.parse_args(argv)
    if args.boost_dev is not None and args.redraw_dev:
        parser.error("--boost-dev and --redraw-dev cannot be combined")
    if args.boost_dev is not None and args.boost_dev < 1:
        parser.error("--boost-dev needs a positive size")
    return args


def lines_of(
    result: sampling.Result, rules_source: str, senders_source: str | None = None
) -> list[str]:
    pool = result.pool
    out = [f"rules: {rules_source}"]
    if senders_source is not None:
        out.append(f"boost senders: {senders_source}")
        out.append(f"stratum: {sampling.BOOST}")
    out += [
        f"set version: {result.version}",
        f"candidates: {len(pool.candidates)}",
        f"not labelable (left out): {pool.unlabelable}",
        f"in an earlier draw (left out): {pool.drawn_before}",
    ]
    for key, count in result.available().items():
        out.append(f"  available {key}: {count}")
    for name, per in result.counts().items():
        out.append(f"{name}: {sum(per.values())}")
        for key, count in per.items():
            out.append(f"  {key}: {count}")
    out.append("recorded" if result.written else "dry run: nothing recorded (add --write)")
    return out


def short_lines(err: sampling.Short) -> list[str]:
    out = ["not enough mail; nothing drawn"]
    for s in err.found:
        out.append(f"  {s.role}/{s.stratum}: need {s.needed}, have {s.available}")
    return out


def boost_of(settings: Settings, size: int) -> sampling.Boost:
    """The local sender list, or the fictional example list when it is absent."""
    path = boost.senders_path(Path(settings.sinan_boost_senders))
    source = "example" if path == boost.EXAMPLE_SENDERS else "local"
    senders = boost.load_senders(path)
    return sampling.Boost(senders, source, size)


def mode_of(args: argparse.Namespace) -> sampling.Mode:
    if args.boost_dev is not None:
        return "boost-dev"
    if args.redraw_dev:
        return "redraw-dev"
    return "first"


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    settings = get_settings()
    path = rules_path(settings)
    source = "example" if path == EXAMPLE_RULES else "local"
    rules = load_rules(path)
    mode = mode_of(args)
    extra = boost_of(settings, args.boost_dev) if mode == "boost-dev" else None
    with Session(get_engine()) as session:
        try:
            result = sampling.run(session, rules, args.seed, mode, args.write, source, extra)
        except sampling.Short as err:
            print("\n".join(short_lines(err)))
            return 1
        except sampling.DrawError as err:
            print(f"refused: {err}")
            return 1
        session.commit()
    senders = extra.source if extra else None
    print("\n".join(lines_of(result, source, senders)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
