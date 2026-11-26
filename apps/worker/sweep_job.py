"""The Worker's proposal sweep, at the end of every cycle.

It records proposals that expired or were superseded (ADR 0009, decision 6),
and moves an item whose proposal expired unanswered to needs_review. It used
to run right before the daily digest; the digest stopped with ADR 0022, so
the Worker no longer writes digests.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from app.actions.proposals import sweep
from apps.worker.sync_job import SessionFactory

log = logging.getLogger("sinan.worker.sweep")

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def sweep_proposals(sessions: SessionFactory, clock: Clock = utc_now) -> dict[str, int]:
    """Check every pending proposal. Returns the counts, or {} on failure."""
    try:
        with sessions() as session:
            counts = sweep(session, clock(), "worker")
            session.commit()
    except Exception as exc:
        log.warning("Proposal sweep failed: %s", type(exc).__name__)
        return {}
    if any(counts.values()):
        log.info("Proposals: %s", counts)
    return counts
