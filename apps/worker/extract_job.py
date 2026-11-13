"""The Worker's extraction job, run right after each mail sync.

The model is reached only through the Extractor, which has no tools. In live
mode without an API key, mail still syncs and extraction is skipped.
"""

import logging
from pathlib import Path

from app.config import Settings
from app.db.extract import Counts, extract_pending
from app.extraction.prefilter import EXAMPLE_RULES, Rules, load_rules
from app.providers.llm import Extractor
from apps.worker.sync_job import SessionFactory

log = logging.getLogger("sinan.worker.extract")


def rules_path(settings: Settings) -> Path:
    """The local rules file when it exists, else the fictional example rules."""
    local = Path(settings.sinan_prefilter_rules)
    if local.is_file():
        return local
    return EXAMPLE_RULES


def load_prefilter(settings: Settings) -> Rules:
    path = rules_path(settings)
    source = "example" if path == EXAMPLE_RULES else "local"
    # Fails at startup on a malformed file; the error names the key, not the value
    rules = load_rules(path)
    log.info("Prefilter rules: %s", source)
    return rules


def extract_all(
    sessions: SessionFactory,
    extractor: Extractor | None,
    rules: Rules,
    cap: int,
    ttl_days: int = 7,
) -> Counts | None:
    """Run one extraction pass. Returns its counts, or None when it did not run."""
    if extractor is None:
        log.info("Extraction skipped: no ANTHROPIC_API_KEY")
        return None
    with sessions() as session:
        try:
            counts = extract_pending(session, extractor, rules, cap, ttl_days)
        except Exception as exc:
            # Class name only: exception text could quote mail content
            log.warning("Extraction failed: %s", type(exc).__name__)
            return None
    log.info(
        "Extraction: %d sent, %d skipped, %d items, %d proposals",
        counts.extracted + counts.invalid + counts.errors,
        counts.skipped,
        counts.items,
        counts.proposals,
    )
    return counts
