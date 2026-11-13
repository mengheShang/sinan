"""Worker entry point: scheduled mail sync, prefilter and extraction, then the digest.

The Worker only ever holds the read-only token.
"""

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.db.session import get_engine
from app.extraction.prefilter import Rules
from app.logredact import install_redaction
from app.providers import factory
from app.providers.llm import Extractor
from apps.worker.digest_job import digest_today
from apps.worker.extract_job import extract_all, load_prefilter
from apps.worker.sync_job import ReaderFor, SessionFactory, build_readers, sync_all

log = logging.getLogger("sinan.worker")


def heartbeat() -> None:
    log.info("worker heartbeat")


def cycle(
    sessions: SessionFactory,
    settings: Settings,
    reader_for: ReaderFor,
    extractor: Extractor | None,
    rules: Rules,
) -> None:
    """Sync, then extract, then the digest, so each step sees the one before."""
    sync_all(sessions, reader_for)
    cap = settings.sinan_extract_per_cycle
    extract_all(sessions, extractor, rules, cap, settings.sinan_proposal_ttl_days)
    digest_today(sessions, settings)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    install_redaction()
    settings = get_settings()
    sessions = sessionmaker(get_engine())
    # Fails at startup in live mode when the Google client or key is missing
    reader_for = build_readers(settings)
    extractor = factory.extractor(settings)
    rules = load_prefilter(settings)
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(heartbeat, "interval", seconds=60, id="heartbeat")
    scheduler.add_job(
        cycle,
        "interval",
        args=[sessions, settings, reader_for, extractor, rules],
        minutes=settings.sinan_sync_interval_minutes,
        id="sync",
        # Run once right away, then on the interval; never two runs at once
        next_run_time=datetime.now(UTC),
        max_instances=1,
        coalesce=True,
    )
    log.info("worker started in %s mode", settings.sinan_mode)
    scheduler.start()


if __name__ == "__main__":
    main()
