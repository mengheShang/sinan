"""Load the real dev set from the label table, split into its parts (Issue #78).

The dev set is the current dev set version: the random draw of ADR 0012 and,
when it was drawn, the supplement of mail likely to hold a deadline (ADR 0018).
Each sample is put in a part by the stratum its email was drawn in:
`deadline_boost` for the supplement, `random` for everything else.

The supplement is chosen by keywords, so it holds more deadlines than mail at
large. Read precision and the notice share from the `random` part.

Samples hold real mail: nothing here logs or prints them.

Snapshots taken before ADR 0010 hold no `Authentication-Results`, so sender
trust cannot be judged from them. `with_auth` reads that one header again
from Gmail, with the Worker's read-only reader, and adds it to the samples
in memory only. Nothing is written back.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.db import labels, sampling
from app.db.label_format import Sample
from app.db.models import Message
from app.extraction.trust import AUTH_HEADER
from app.providers.mail import MailProviderError, MailReader, MessageNotFoundError
from evals.dataset import SetError

# A read-only reader for one account, or None when it cannot read now
ReaderFor = Callable[[Session, uuid.UUID, str], MailReader | None]

RANDOM = "random"
BOOST = sampling.BOOST


@dataclass(frozen=True)
class RealSet:
    set_version: int
    samples: list[Sample]
    # Sample id to part name: `random` or `deadline_boost`
    parts: dict[str, str]
    # Sample id to source message id
    sources: dict[str, uuid.UUID]


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
    return RealSet(version, samples, parts, sources)


@dataclass(frozen=True)
class AuthFetch:
    samples: list[Sample]
    # Samples whose header was read again, and those that could not be
    fetched: int
    missing: int


def auth_of(reader: MailReader, provider_msg_id: str) -> str | None:
    try:
        msg = reader.get_message(provider_msg_id)
    except (MessageNotFoundError, MailProviderError):
        return None
    return msg.headers.get(AUTH_HEADER)


def with_header(sample: Sample, value: str) -> Sample:
    headers = {**sample.input.headers, AUTH_HEADER: value}
    snap = sample.input.model_copy(update={"headers": headers})
    return sample.model_copy(update={"input": snap})


def with_auth(session: Session, found: RealSet, reader_for: ReaderFor) -> AuthFetch:
    """The samples with Gmail's authentication results added, in memory only."""
    readers: dict[uuid.UUID, MailReader | None] = {}
    out = []
    missing = 0
    for sample in found.samples:
        msg = session.get(Message, found.sources[sample.id])
        value = None
        if msg is not None:
            if msg.account_id not in readers:
                account = msg.account_id
                readers[account] = reader_for(session, account, "")
            reader = readers[msg.account_id]
            value = auth_of(reader, msg.provider_msg_id) if reader else None
        if value is None:
            missing += 1
            out.append(sample)
            continue
        out.append(with_header(sample, value))
    return AuthFetch(out, len(out) - missing, missing)
