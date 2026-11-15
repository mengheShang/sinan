"""Load the real dev set from the label table, split into its parts (Issue #78).

The dev set is the current dev set version: the random draw of ADR 0012 and,
when it was drawn, the supplement of mail likely to hold a deadline (ADR 0018).
Each sample is put in a part by the stratum its email was drawn in:
`deadline_boost` for the supplement, `random` for everything else.

The supplement is chosen by keywords, so it holds more deadlines than mail at
large. Read precision and the notice share from the `random` part.

Samples hold real mail: nothing here logs or prints them.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.db import labels, sampling
from app.db.label_format import Sample
from evals.dataset import SetError

RANDOM = "random"
BOOST = sampling.BOOST


@dataclass(frozen=True)
class RealSet:
    set_version: int
    samples: list[Sample]
    # Sample id to part name: `random` or `deadline_boost`
    parts: dict[str, str]


def part_of(stratum: str | None) -> str:
    return BOOST if stratum == BOOST else RANDOM


def load_dev(session: Session) -> RealSet:
    """The current dev set version, its samples and their parts."""
    version = sampling.latest_versions(session).get("dev")
    if version is None:
        raise SetError("no dev set has been drawn")
    samples = labels.load_set(session, "dev", set_version=version)
    if not samples:
        raise SetError(f"dev v{version} has no labels yet")
    strata = sampling.strata(session, "dev", version)
    sources = labels.sources(session, "dev", version)
    parts = {}
    for sample in samples:
        stratum = strata.get(sources[sample.id])
        parts[sample.id] = part_of(stratum)
    return RealSet(version, samples, parts)
