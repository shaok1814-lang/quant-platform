"""APScheduler launcher (W6.1.5 + W6.5 weekly paper).

Wraps :class:`apscheduler.schedulers.blocking.BlockingScheduler` so
``python -m ops`` (via :mod:`ops.__main__`) starts a single-process
loop that runs two jobs:

  * :func:`ops.ingest_job.run_daily_ingest` — daily 18:00 Asia/Shanghai
    (post-market close + index publication lag).
  * :func:`ops.weekly_paper_job.run_weekly_paper_session` — Sunday
    9:00 Asia/Shanghai. Enforces CLAUDE.md 「实盘前必须经过至少
    4 周模拟盘验证」 by recording a fresh paper session every
    week.

Design choices (rationale on each):

  * **BlockingScheduler**, not BackgroundScheduler. W6.1 ships a
    single-process scheduler that should run as the program's only
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
  * **Weekly paper slot = Sunday 9:00 Asia/Shanghai**. Weekday
    mornings BEFORE the market open (9:30 open; 9:00 lets the
    scheduler fit a single-digit-second run without racing the
    open auction). Earlier in the week than the daily 18:00 hot
    path so they never collide; weekly cadence means even with
    a missed Sunday the operator gets a paper trace within 7
    days of intent. W6.5.
"""

from __future__ import annotations

from typing import Final

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from ops.ingest_job import run_daily_ingest
from ops.weekly_paper_job import run_weekly_paper_session

__all__ = [
    "DEFAULT_HOUR",
    "DEFAULT_MINUTE",
    "DEFAULT_TZ",
    "DEFAULT_WEEKLY_DAY_OF_WEEK",
    "DEFAULT_WEEKLY_HOUR",
    "DEFAULT_WEEKLY_MINUTE",
    "build_scheduler",
]

# 18:00 Asia/Shanghai is the daily ingest target: post-market close
# (15:00 CST) plus ~3 hours for index publication / TAQ data.
DEFAULT_HOUR: Final[int] = 18
DEFAULT_MINUTE: Final[int] = 0
DEFAULT_TZ: Final[str] = "Asia/Shanghai"

# Sunday 9:00 Asia/Shanghai is the weekly paper-validation target.
# ``day_of_week="sun"`` in APScheduler's CronTrigger (English
# 3-letter). Runs BEFORE market open (9:30) so the report is
# available when the trading day starts.
DEFAULT_WEEKLY_DAY_OF_WEEK: Final[str] = "sun"
DEFAULT_WEEKLY_HOUR: Final[int] = 9
DEFAULT_WEEKLY_MINUTE: Final[int] = 0


def build_scheduler(
    *,
    hour: int = DEFAULT_HOUR,
    minute: int = DEFAULT_MINUTE,
    timezone: str = DEFAULT_TZ,
    enable_weekly_paper: bool = True,
    weekly_day_of_week: str = DEFAULT_WEEKLY_DAY_OF_WEEK,
    weekly_hour: int = DEFAULT_WEEKLY_HOUR,
    weekly_minute: int = DEFAULT_WEEKLY_MINUTE,
) -> BlockingScheduler:
    """Build a :class:`BlockingScheduler` with the daily ingest +
    weekly paper-validation jobs.

    Args:
        hour: Local hour (0-23) at which the daily ingest fires.
        minute: Local minute (0-59) at which the daily ingest fires.
        timezone: IANA tz string. Default ``"Asia/Shanghai"``.
            ApScheduler raises if the tz is unknown on the host.
        enable_weekly_paper: If ``True`` (default), register the
            weekly paper-validation job. Set ``False`` for tests
            that only want the daily ingest path.
        weekly_day_of_week: APScheduler ``day_of_week`` value for
            the weekly slot. Default ``"sun"`` (Sunday).
        weekly_hour: Local hour for the weekly slot. Default 9.
        weekly_minute: Local minute for the weekly slot. Default 0.

    Returns:
        A :class:`BlockingScheduler` with one or two jobs registered::

            id="daily_ingest"
            func=ops.ingest_job.run_daily_ingest
            trigger=cron(hour, minute, timezone)

            id="weekly_paper"   (if enable_weekly_paper)
            func=ops.weekly_paper_job.run_weekly_paper_session
            trigger=cron(weekly_day_of_week, weekly_hour, weekly_minute, timezone)

        Caller calls ``.start()`` to enter the blocking main loop.

    Notes:
        The function is NOT registered as a module-level scheduler
        so :func:`unittest.mock.patch` on the underlying ``run_*``
        functions can replace them without bringing up APScheduler —
        see ``tests/test_scheduler.py``.
    """
    scheduler = BlockingScheduler(timezone=timezone)
    scheduler.add_job(
        run_daily_ingest,
        CronTrigger(hour=hour, minute=minute, timezone=timezone),
        id="daily_ingest",
        kwargs={"include_delisted": True},
        name="Daily OHLCV ingest",
        # ``misfire_grace_time=None`` → APScheduler's default
        # (no grace window); a missed run is dropped, the next
        # daily slot still fires normally. Avoids backlog
        # storms after a long outage.
        coalesce=False,
        max_instances=1,
    )
    if enable_weekly_paper:
        scheduler.add_job(
            run_weekly_paper_session,
            CronTrigger(
                day_of_week=weekly_day_of_week,
                hour=weekly_hour,
                minute=weekly_minute,
                timezone=timezone,
            ),
            id="weekly_paper",
            name="Weekly paper-validation session",
            coalesce=False,
            max_instances=1,
        )
    logger.info(
        "scheduler built: daily_ingest at {h:02d}:{m:02d} {tz}; "
        "weekly_paper={wp} at {wh:02d}:{wm:02d} {tz}",
        h=hour,
        m=minute,
        tz=timezone,
        wp=("on" if enable_weekly_paper else "OFF"),
        wh=weekly_hour,
        wm=weekly_minute,
    )
    return scheduler
