"""Worker 入口：定时同步和提取。

M0 只运行一个心跳任务。Worker 永远只持有只读令牌。
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
