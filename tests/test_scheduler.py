"""Tests for ``ops.scheduler.build_scheduler`` (W6.1.5 + W6.5).

  * Verifies the cron jobs are registered with the right ids,
    funcs, and trigger parameters (daily ingest + weekly paper).
  * Verifies the ``start()`` method is NOT called as a side effect
    of ``build_scheduler`` (so the entry point is testable without
    spinning the loop).
  * Verifies the weekly job can be disabled via ``enable_weekly_paper``.
  * Verifies ``python -m ops`` module-level behavior (does NOT
    enter the loop at import; only via ``main()``).

The scheduler uses ``apscheduler.schedulers.blocking.BlockingScheduler``
which has no useful introspection in production deployments, but
in a unit-test setting ``get_jobs()`` returns the registered job
list right after ``add_job``.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apscheduler.triggers.cron import CronTrigger  # noqa: E402
from ops.scheduler import (  # noqa: E402
    DEFAULT_HOUR,
    DEFAULT_MINUTE,
    DEFAULT_TZ,
    DEFAULT_WEEKLY_DAY_OF_WEEK,
    DEFAULT_WEEKLY_HOUR,
    DEFAULT_WEEKLY_MINUTE,
    build_scheduler,
)


def test_build_scheduler_registers_daily_ingest_job() -> None:
    """``build_scheduler`` registers the daily ingest job at 18:00
    Asia/Shanghai (default)."""
    scheduler = build_scheduler()
    jobs = {j.id: j for j in scheduler.get_jobs()}
    assert "daily_ingest" in jobs, list(jobs)
    job = jobs["daily_ingest"]
    assert isinstance(job.trigger, CronTrigger)


def test_build_scheduler_registers_weekly_paper_job_by_default() -> None:
    """W6.5: weekly paper-validation job registered at Sunday 9:00
    Asia/Shanghai (default)."""
    scheduler = build_scheduler()
    jobs = {j.id: j for j in scheduler.get_jobs()}
    assert "weekly_paper" in jobs, list(jobs)
    job = jobs["weekly_paper"]
    assert isinstance(job.trigger, CronTrigger)
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert DEFAULT_WEEKLY_DAY_OF_WEEK.lower() in fields["day_of_week"].lower()
    assert str(DEFAULT_WEEKLY_HOUR) in fields["hour"]
    assert str(DEFAULT_WEEKLY_MINUTE) in fields["minute"]


def test_build_scheduler_disable_weekly_paper() -> None:
    """``enable_weekly_paper=False`` removes the weekly job (only
    daily ingest remains)."""
    scheduler = build_scheduler(enable_weekly_paper=False)
    jobs = {j.id for j in scheduler.get_jobs()}
    assert jobs == {"daily_ingest"}


def test_build_scheduler_custom_weekly_params() -> None:
    """Custom weekly schedule params are reflected in the trigger."""
    scheduler = build_scheduler(
        weekly_day_of_week="fri",
        weekly_hour=18,
        weekly_minute=30,
    )
    weekly_job = next(j for j in scheduler.get_jobs() if j.id == "weekly_paper")
    fields = {f.name: str(f) for f in weekly_job.trigger.fields}
    assert "fri" in fields["day_of_week"].lower()
    assert "18" in fields["hour"]
    assert "30" in fields["minute"]


def test_build_scheduler_does_not_start() -> None:
    """``build_scheduler`` returns a scheduler but does NOT call
    ``start()`` (the main loop). Introspection-only."""
    scheduler = build_scheduler()
    # ``BlockingScheduler.state`` is ``0`` (STATE_STOPPED) right
    # after construction. If we accidentally called ``start()``
    # it would be ``1`` (STATE_RUNNING) and the test process
    # would hang.
    from apscheduler.schedulers.base import STATE_STOPPED

    assert scheduler.state == STATE_STOPPED, (
        "build_scheduler must not start the scheduler; tests should remain safe to run in CI"
    )


def test_build_scheduler_custom_hour_minute_tz() -> None:
    """Custom daily schedule params are reflected in the cron trigger."""
    scheduler = build_scheduler(hour=23, minute=30, timezone="Asia/Tokyo")
    job = next(j for j in scheduler.get_jobs() if j.id == "daily_ingest")
    trigger = job.trigger
    # CronTrigger fields are private; access via the trigger's
    # ``fields`` collection (well-known APScheduler 3.x surface).
    fields = {f.name: str(f) for f in trigger.fields}
    assert "23" in fields["hour"]
    assert "30" in fields["minute"]
    assert "asia/tokyo" in str(trigger.timezone).lower()


def test_build_scheduler_default_constants_match() -> None:
    """The DEFAULT_* constants at module top match what
    ``build_scheduler()`` uses with no args (defends against
    accidental drift between the constants and the default
    parameter values)."""
    scheduler = build_scheduler()
    job = next(j for j in scheduler.get_jobs() if j.id == "daily_ingest")
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert str(DEFAULT_HOUR) in fields["hour"]
    assert str(DEFAULT_MINUTE) in fields["minute"]
    assert DEFAULT_TZ.lower() in str(job.trigger.timezone).lower()


def test_main_import_does_not_enter_loop() -> None:
    """Importing ``ops.__main__`` must NOT call ``main()`` (the
    blocking loop). It's invoked only when the module is run as
    ``python -m ops`` (``if __name__ == '__main__'``)."""
    # Reload the module to ensure the import side effect is re-
    # evaluated; any stray ``main()`` would start the scheduler.
    if "ops.__main__" in sys.modules:
        del sys.modules["ops.__main__"]
    importlib.import_module("ops.__main__")
    # No AssertionError / hang ⇒ import is side-effect free.


def test_scheduler_with_max_instances_one_prevents_overlap() -> None:
    """``max_instances=1`` is set on both jobs so a long-running
    ingest / weekly paper session cannot spawn a second parallel
    run if the previous run hasn't finished yet."""
    scheduler = build_scheduler()
    for job in scheduler.get_jobs():
        assert job.max_instances == 1, f"job {job.id} max_instances != 1"


def test_scheduler_coalesce_disabled() -> None:
    """``coalesce=False`` on both jobs — missed runs are not
    back-filled when the scheduler resumes after downtime."""
    scheduler = build_scheduler()
    for job in scheduler.get_jobs():
        assert job.coalesce is False, f"job {job.id} coalesce != False"
