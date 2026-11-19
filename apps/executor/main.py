"""Executor entry point: runs approved or granted actions.

This is the only module that can write to external services. It reads
ExecutorSettings, not Settings: it holds the private key that opens write
and calendar tokens, and never the key for read tokens (ADR 0021,
decision 1). It still executes no actions; the job loop comes with #99.
"""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from app.config import ExecutorSettings
from app.logredact import install_redaction

log = logging.getLogger("sinan.executor")


def heartbeat() -> None:
    log.info("executor heartbeat")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    install_redaction()
    # Refuses to start on a malformed key or when given the read-token key
    settings = ExecutorSettings()
    if settings.sinan_mode == "live" and settings.sinan_executor_private_key is None:
        log.warning("SINAN_EXECUTOR_PRIVATE_KEY is not set: actions stay off")
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(heartbeat, "interval", seconds=60, id="heartbeat")
    log.info("executor started")
    scheduler.start()


if __name__ == "__main__":
    main()
