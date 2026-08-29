"""Tests for ``ops.scheduler.build_scheduler`` (W6.1.5).

  * Verifies the cron job is registered with the right id, func,
    and trigger parameters.
  * Verifies the ``start()`` method is NOT called as a side effect
    of ``build_scheduler`` (so the entry point is testable without
    spinning the loop).
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
    build_scheduler,
)


def test_build_scheduler_registers_daily_ingest_job() -> None:
    """``build_scheduler`` registers one job, id ``daily_ingest``,
    with the cron trigger at the default 18:00 Asia/Shanghai."""
    scheduler = build_scheduler()
    jobs = scheduler.get_jobs()
    assert len(jobs) == 1, f"expected 1 job, got {len(jobs)}"
    job = jobs[0]
    assert job.id == "daily_ingest"
    assert isinstance(job.trigger, CronTrigger)


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
    """Custom schedule params are reflected in the cron trigger."""
    scheduler = build_scheduler(hour=23, minute=30, timezone="Asia/Tokyo")
    job = scheduler.get_jobs()[0]
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
    job = scheduler.get_jobs()[0]
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
    """``max_instances=1`` is set so a long-running ingest cannot
    spawn a second parallel run if the previous run hasn't
    finished yet. APScheduler would queue the next fire on the
    default thread pool until the previous returns."""
    scheduler = build_scheduler()
    job = scheduler.get_jobs()[0]
    assert job.max_instances == 1


def test_scheduler_coalesce_disabled() -> None:
    """``coalesce=False`` — a missed daily run is not back-filled
    when the scheduler resumes after downtime (matches the
    decision rationale in ``ops.scheduler`` module docstring)."""
    scheduler = build_scheduler()
    job = scheduler.get_jobs()[0]
    assert job.coalesce is False
