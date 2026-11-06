"""Executor entry point: runs approved or granted actions.

This is the only module that can write to external services.
M0 only runs a heartbeat job and executes no actions.
"""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from app.logredact import install_redaction

log = logging.getLogger("sinan.executor")


def heartbeat() -> None:
    log.info("executor heartbeat")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    install_redaction()
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(heartbeat, "interval", seconds=60, id="heartbeat")
    log.info("executor started")
    scheduler.start()


if __name__ == "__main__":
    main()
