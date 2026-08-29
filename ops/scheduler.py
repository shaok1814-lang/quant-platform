"""APScheduler launcher (W6.1.5).

Wraps :class:`apscheduler.schedulers.blocking.BlockingScheduler` so
``python -m ops`` (via :mod:`ops.__main__`) starts a single-process
loop that runs :func:`ops.ingest_job.run_daily_ingest` once per day
at a configurable local time (default 18:00 Asia/Shanghai, post-market).

Design choices (rationale on each):

  * **BlockingScheduler**, not BackgroundScheduler. W6.1 ships a
    single-job scheduler that should run as the program's only
    long-lived activity (``python -m ops`` from
    ``task scheduler`` / Windows task scheduler / launchd). The
    blocking variant is the right primitive. Adding a second
    scheduler that lives alongside another main loop is a future
    need (a ``Streamlit`` / ``fastapi`` embed) and won't share
    this code path.
  * **Cron trigger** rather than interval. Interval schedules drift
    on DST / clock adjustments; cron is anchored to wall-clock
    time which is what a "daily at 18:00 local" semantic needs.
  * **Asia/Shanghai by default**. A-share post-market ingestion at
    18:00 CST post-dates official market close (15:00 CST) plus
    the 15:00-17:00 index publication window. Override via
    :data:`DEFAULT_TZ` if needed.
  * **misfire_grace_time = None**. A missed daily run is missed;
    APScheduler's coalesce will not stack-fire later runs on the
    next wake. The next day's job still runs at 18:00. (Forcing
    catch-up on resume can blast a holiday backlog in one batch.)
"""

from __future__ import annotations

from typing import Final

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from ops.ingest_job import run_daily_ingest

__all__ = [
    "DEFAULT_HOUR",
    "DEFAULT_MINUTE",
    "DEFAULT_TZ",
    "build_scheduler",
]

# 18:00 Asia/Shanghai is the daily ingest target: post-market close
# (15:00 CST) plus ~3 hours for index publication / TAQ data.
DEFAULT_HOUR: Final[int] = 18
DEFAULT_MINUTE: Final[int] = 0
DEFAULT_TZ: Final[str] = "Asia/Shanghai"


def build_scheduler(
    *,
    hour: int = DEFAULT_HOUR,
    minute: int = DEFAULT_MINUTE,
    timezone: str = DEFAULT_TZ,
) -> BlockingScheduler:
    """Build a :class:`BlockingScheduler` with the daily ingest job.

    Args:
        hour: Local hour (0-23) at which to fire.
        minute: Local minute (0-59) at which to fire.
        timezone: IANA tz string. Default ``"Asia/Shanghai"``.
            ApScheduler raises if the tz is unknown on the host.

    Returns:
        A :class:`BlockingScheduler` with one job registered::

            id="daily_ingest"
            func=ops.ingest_job.run_daily_ingest
            trigger=cron(hour, minute, timezone)

        Caller calls ``.start()`` to enter the blocking main loop.

    Notes:
        The function is NOT registered as a module-level scheduler
        so :func:`unittest.mock.patch` on ``ingest_job.run_daily_ingest``
        can replace it without bringing up APScheduler — see
        ``tests/test_scheduler.py``.
    """
    scheduler = BlockingScheduler(timezone=timezone)
    scheduler.add_job(
        run_daily_ingest,
        CronTrigger(hour=hour, minute=minute, timezone=timezone),
        id="daily_ingest",
        name="Daily OHLCV ingest",
        # ``misfire_grace_time=None`` → APScheduler's default
        # (no grace window); a missed run is dropped, the next
        # daily slot still fires normally. Avoids backlog
        # storms after a long outage.
        coalesce=False,
        max_instances=1,
    )
    logger.info(
        "scheduler built: daily_ingest at {h:02d}:{m:02d} {tz}",
        h=hour,
        m=minute,
        tz=timezone,
    )
    return scheduler
