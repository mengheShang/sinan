"""Worker entry point: scheduled mail sync, and extraction later (#13).

The Worker only ever holds the read-only token.
"""

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.session import get_engine
from app.logredact import install_redaction
from apps.worker.sync_job import build_readers, sync_all

log = logging.getLogger("sinan.worker")


def heartbeat() -> None:
    log.info("worker heartbeat")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    install_redaction()
    settings = get_settings()
    sessions = sessionmaker(get_engine())
    # Fails at startup in live mode when the Google client or key is missing
    reader_for = build_readers(settings)
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(heartbeat, "interval", seconds=60, id="heartbeat")
    scheduler.add_job(
        sync_all,
        "interval",
        args=[sessions, reader_for],
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
