"""Executor 入口：执行已批准或已授权的动作。

这是唯一能写外部服务的模块。M0 只运行一个心跳任务，不执行任何动作。
"""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

log = logging.getLogger("sinan.executor")


def heartbeat() -> None:
    log.info("executor heartbeat")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(heartbeat, "interval", seconds=60, id="heartbeat")
    log.info("executor started")
    scheduler.start()


if __name__ == "__main__":
    main()
