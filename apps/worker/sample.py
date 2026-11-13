"""Command line for drawing the real development and test sets (Issue #41).

The owner runs it in the Worker container, which has the database and the
local prefilter rules:

    docker compose exec worker python -m apps.worker.sample --seed 20261112
    docker compose exec worker python -m apps.worker.sample --seed 20261112 --write

Without --write it only shows what it would draw. With --write it records the
draw. A second run is refused: the test set is fixed once drawn. A new dev set
is drawn with --redraw-dev.

It prints counts only, never an address, a subject or a body.
"""

import argparse
import sys
from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import sampling
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
    return parser.parse_args(argv)


def lines_of(result: sampling.Result, rules_source: str) -> list[str]:
    pool = result.pool
    out = [
        f"rules: {rules_source}",
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


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    settings = get_settings()
    path = rules_path(settings)
    source = "example" if path == EXAMPLE_RULES else "local"
    rules = load_rules(path)
    mode: sampling.Mode = "redraw-dev" if args.redraw_dev else "first"
    with Session(get_engine()) as session:
        try:
            result = sampling.run(session, rules, args.seed, mode, args.write, source)
        except sampling.Short as err:
            print("\n".join(short_lines(err)))
            return 1
        except sampling.DrawError as err:
            print(f"refused: {err}")
            return 1
        session.commit()
    print("\n".join(lines_of(result, source)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
