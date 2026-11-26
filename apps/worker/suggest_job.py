"""The Worker's due-time suggestions, after extraction (ADR 0022, decision 4).

In live mode without an API key there is no suggester and the step is
skipped; demo mode uses a fake that suggests nothing. A suggestion is only
stored; it changes no item.
"""

import logging
from datetime import tzinfo

from app.db.suggest import SuggestCounts, suggest_pending
from app.providers.llm import Suggester
from apps.worker.sync_job import SessionFactory

log = logging.getLogger("sinan.worker.suggest")


def suggest_all(
    sessions: SessionFactory, suggester: Suggester | None, cap: int, zone: tzinfo
) -> SuggestCounts | None:
    """Ask about up to `cap` items. Returns the counts, or None when skipped."""
    if suggester is None or cap == 0:
        return None
    try:
        with sessions() as session:
            counts = suggest_pending(session, suggester, cap, zone)
    except Exception as exc:
        log.warning("Due suggestions failed: %s", type(exc).__name__)
        return None
    if counts.asked or counts.stopped:
        log.info(
            "Due suggestions: %d asked, %d suggested, stopped=%s",
            counts.asked,
            counts.suggested,
            counts.stopped,
        )
    return counts
