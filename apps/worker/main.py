"""Worker entry point: scheduled sync and extraction.

M0 only runs a heartbeat job. The Worker only ever holds the read-only token.
"""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

log = logging.getLogger("sinan.worker")


def heartbeat() -> None:
    log.info("worker heartbeat")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(heartbeat, "interval", seconds=60, id="heartbeat")
    log.info("worker started")
    scheduler.start()


if __name__ == "__main__":
    main()
