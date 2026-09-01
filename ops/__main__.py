"""``python -m ops`` entry point.

Builds the scheduler and enters the blocking main loop.
The scheduler runs until interrupted (SIGINT / SIGTERM /
``KeyboardInterrupt`` from Ctrl+C). On Windows + APScheduler
3.x, ``BlockingScheduler.start()`` returns cleanly on Ctrl+C.

Override defaults via env vars:

  * ``OPS_INGEST_HOUR`` (int, default 18) — daily ingest hour
  * ``OPS_INGEST_MINUTE`` (int, default 0) — daily ingest minute
  * ``OPS_INGEST_TZ`` (str, default ``"Asia/Shanghai"``)
  * ``OPS_WEEKLY_PAPER_ENABLED`` (str, default ``"1"``) — set to
    ``"0"`` / ``"false"`` / ``"no"`` to disable the weekly paper
    job (W6.5).
  * ``OPS_WEEKLY_PAPER_DAY`` (str, default ``"sun"``) — APScheduler
    ``day_of_week`` value (e.g. ``"mon"`` / ``"fri"``).
  * ``OPS_WEEKLY_PAPER_HOUR`` (int, default 9)
  * ``OPS_WEEKLY_PAPER_MINUTE`` (int, default 0)

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
    DEFAULT_WEEKLY_DAY_OF_WEEK,
    DEFAULT_WEEKLY_HOUR,
    DEFAULT_WEEKLY_MINUTE,
    build_scheduler,
)


def _truthy(value: str | None, default: bool = True) -> bool:
    """Parse an env-var as boolean. Treats ``"0"`` / ``"false"`` /
    ``"no"`` (case-insensitive) as False; everything else (including
    ``None`` → default) is True.
    """
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


def main() -> int:
    """Run the dual-job scheduler (daily ingest + weekly paper)
    until interrupted."""
    hour = int(os.environ.get("OPS_INGEST_HOUR", DEFAULT_HOUR))
    minute = int(os.environ.get("OPS_INGEST_MINUTE", DEFAULT_MINUTE))
    timezone = os.environ.get("OPS_INGEST_TZ", DEFAULT_TZ)
    enable_weekly_paper = _truthy(
        os.environ.get("OPS_WEEKLY_PAPER_ENABLED"),
        default=True,
    )
    weekly_day = os.environ.get(
        "OPS_WEEKLY_PAPER_DAY", DEFAULT_WEEKLY_DAY_OF_WEEK,
    )
    weekly_hour = int(
        os.environ.get("OPS_WEEKLY_PAPER_HOUR", DEFAULT_WEEKLY_HOUR),
    )
    weekly_minute = int(
        os.environ.get("OPS_WEEKLY_PAPER_MINUTE", DEFAULT_WEEKLY_MINUTE),
    )

    scheduler = build_scheduler(
        hour=hour,
        minute=minute,
        timezone=timezone,
        enable_weekly_paper=enable_weekly_paper,
        weekly_day_of_week=weekly_day,
        weekly_hour=weekly_hour,
        weekly_minute=weekly_minute,
    )

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
