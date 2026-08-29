"""``python -m ops`` entry point.

Builds the scheduler and enters the blocking main loop.
The scheduler runs until interrupted (SIGINT / SIGTERM /
``KeyboardInterrupt`` from Ctrl+C). On Windows + APScheduler
3.x, ``BlockingScheduler.start()`` returns cleanly on Ctrl+C.

Override defaults via env vars:

  * ``OPS_INGEST_HOUR`` (int, default 18)
  * ``OPS_INGEST_MINUTE`` (int, default 0)
  * ``OPS_INGEST_TZ`` (str, default ``"Asia/Shanghai"``)

The env-var indirection lets ops teams configure the schedule
without code changes (CLAUDE.md: 用配置而非硬编码).
"""

from __future__ import annotations

import os
import sys

from loguru import logger

from ops.scheduler import (
    DEFAULT_HOUR,
    DEFAULT_MINUTE,
    DEFAULT_TZ,
    build_scheduler,
)


def main() -> int:
    """Run the daily ingest scheduler until interrupted."""
    hour = int(os.environ.get("OPS_INGEST_HOUR", DEFAULT_HOUR))
    minute = int(os.environ.get("OPS_INGEST_MINUTE", DEFAULT_MINUTE))
    timezone = os.environ.get("OPS_INGEST_TZ", DEFAULT_TZ)

    scheduler = build_scheduler(hour=hour, minute=minute, timezone=timezone)

    # APScheduler's BlockingScheduler.start() installs a SIGINT
    # handler on Linux/macOS that raises KeyboardInterrupt on
    # next loop iteration; on Windows it relies on the default
    # Ctrl+C behavior. Either way KeyboardInterrupt bubbles up
    # to ``except`` below and we exit cleanly.
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler interrupted; shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
