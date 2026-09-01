"""Tests for ``scripts/run_paper_validation.py`` (D: 4-week launch).

Pre-flight + smoke. Each check is independently verifiable; the
smoke test runs the full pipeline against a stub DuckDB so we
know ``run_paper_validation.py`` can drive a multi-week simulation
end-to-end without manual intervention.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

# Import the script as a module. The script's top-level
# ``if __name__ == "__main__"`` doesn't fire when imported.
import scripts.run_paper_validation as pv  # noqa: E402
from data_layer.storage.duck import DuckStore  # noqa: E402

# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------


def test_check_duckdb_exists_returns_true_when_present(tmp_path: Path) -> None:
    db = tmp_path / "exists.duckdb"
    # Create the file (empty DuckDB is fine for the existence check).
    db.write_bytes(b"")
    # Patch DEFAULT_DUCKDB_PATH at the source.
    import ops.weekly_paper_job as wjob

    orig = wjob.DEFAULT_DUCKDB_PATH
    wjob.DEFAULT_DUCKDB_PATH = db
    try:
        assert pv.check_duckdb_exists() is True
    finally:
        wjob.DEFAULT_DUCKDB_PATH = orig


def test_check_duckdb_exists_returns_false_when_missing(tmp_path: Path) -> None:
    import ops.weekly_paper_job as wjob

    orig = wjob.DEFAULT_DUCKDB_PATH
    wjob.DEFAULT_DUCKDB_PATH = tmp_path / "no-such.duckdb"
    try:
        assert pv.check_duckdb_exists() is False
    finally:
        wjob.DEFAULT_DUCKDB_PATH = orig


def test_check_dingtalk_env_ok_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/...")
    assert pv.check_dingtalk_env() is True


def test_check_dingtalk_env_warn_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DINGTALK_WEBHOOK_URL", raising=False)
    assert pv.check_dingtalk_env() is False  # WARN, not FAIL


def test_check_scheduler_build_default() -> None:
    """Default kwargs build cleanly with 2 jobs (daily_ingest + weekly_paper)."""
    from ops.scheduler import build_scheduler

    # The check uses the production build_scheduler; ensure it
    # returns 2 jobs. Don't mutate global state.
    sched = build_scheduler()
    assert len(sched.get_jobs()) == 2
    assert pv.check_scheduler_build() is True


def test_check_scheduler_build_handles_disable() -> None:
    """With weekly disabled, check_scheduler_build correctly FAILS."""
    import ops.scheduler as sched_mod

    # Monkey-patch build_scheduler to return a scheduler with
    # only 1 job.
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    def fake_build(*args, **kwargs) -> BlockingScheduler:  # type: ignore[no-untyped-def]
        s = BlockingScheduler(timezone="Asia/Shanghai")
        s.add_job(
            lambda: None,
            CronTrigger(hour=0, minute=0),
            id="daily_ingest",
            name="Daily",
            coalesce=False,
            max_instances=1,
        )
        return s

    orig = sched_mod.build_scheduler
    sched_mod.build_scheduler = fake_build
    try:
        assert pv.check_scheduler_build() is False
    finally:
        sched_mod.build_scheduler = orig


# ---------------------------------------------------------------------------
# Smoke (multi-week)
# ---------------------------------------------------------------------------


def _populate_dummy_duckdb(db: Path, symbol: str, n: int) -> None:
    """Seed ``db`` with ``n`` business days of OHLCV for ``symbol``."""
    from datetime import UTC, datetime, timedelta

    today = datetime.now(UTC).date()
    dates = pd.bdate_range(end=today - timedelta(days=1), periods=n)
    rows = [
        (
            d.strftime("%Y-%m-%d"),
            10.0 + 0.05 * i,  # open
            10.1 + 0.05 * i,  # high
            9.9 + 0.05 * i,  # low
            10.0 + 0.05 * i,  # close
            1_000_000.0,  # volume
        )
        for i, d in enumerate(dates)
    ]
    df = pd.DataFrame(
        rows,
        columns=["date", "open", "high", "low", "close", "volume"],
    )
    df["date"] = pd.to_datetime(df["date"])
    df["amount"] = df["volume"] * df["close"] * 0.001
    df.attrs["symbol"] = symbol
    df.attrs["fetcher"] = "stub"
    df.attrs["adjust"] = "qfq"
    df.attrs["fetched_at"] = datetime.now(UTC).isoformat()
    with DuckStore(db) as store:
        store.upsert_daily_bars(df)


def test_smoke_runs_n_weeks(tmp_path: Path) -> None:
    """``run_smoke(n)`` invokes the weekly paper job N times.

    Uses a stub DuckDB + stub strategy to avoid the production
    MACrossStrategy import path (the smoke runs in CI).
    """
    db = tmp_path / "daily.duckdb"
    out = tmp_path / "reports"
    _populate_dummy_duckdb(db, symbol="000001", n=30)

    import ops.weekly_paper_job as wjob

    orig_db = wjob.DEFAULT_DUCKDB_PATH
    orig_out = wjob.DEFAULT_OUTPUT_DIR
    wjob.DEFAULT_DUCKDB_PATH = db
    wjob.DEFAULT_OUTPUT_DIR = out
    try:
        # Build a stub strategy class with no AKQuant dependency.
        class _StubStrategy:
            def __init__(self) -> None:
                self._bought = False

            def on_start(self) -> None:
                return None

            def on_bar(self, bar: object) -> None:
                sym = getattr(bar, "symbol", None)
                if sym == "000001" and not self._bought:
                    self._bought = True
                    self.order_target_percent(
                        symbol="000001",
                        target_percent=0.09,
                    )

        rc = pv.run_smoke(3, strategy_cls=_StubStrategy)
        assert rc == 0
        # Smoke re-runs write to the same path; we get 1 file (the
        # last run's report), not 3.
        files = list(out.glob("weekly_*.json"))
        assert len(files) == 1
        on_disk = json.loads(files[0].read_text(encoding="utf-8"))
        assert on_disk["n_filled"] >= 1
    finally:
        wjob.DEFAULT_DUCKDB_PATH = orig_db
        wjob.DEFAULT_OUTPUT_DIR = orig_out


def test_smoke_returns_nonzero_when_weekly_run_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the weekly paper job raises, smoke returns 1 (not 0)."""
    import ops.weekly_paper_job as wjob

    orig_db = wjob.DEFAULT_DUCKDB_PATH
    orig_out = wjob.DEFAULT_OUTPUT_DIR
    wjob.DEFAULT_DUCKDB_PATH = tmp_path / "no-such.duckdb"
    wjob.DEFAULT_OUTPUT_DIR = tmp_path / "reports"

    # Stub strategy isn't even reached because the missing-DB
    # path raises before the bridge construction.
    def _stub() -> type:
        class _S:
            def on_bar(self, bar: object) -> None: ...

        return _S

    try:
        rc = pv.run_smoke(2, strategy_cls=_stub())
        assert rc == 1
    finally:
        wjob.DEFAULT_DUCKDB_PATH = orig_db
        wjob.DEFAULT_OUTPUT_DIR = orig_out


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------


def test_main_preflight_only_fails_on_missing_duckdb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main()`` with no flags → pre-flight only. Returns 1 if any
    blocker fires (here: missing DuckDB)."""
    import ops.weekly_paper_job as wjob

    orig_db = wjob.DEFAULT_DUCKDB_PATH
    wjob.DEFAULT_DUCKDB_PATH = tmp_path / "no-such.duckdb"
    try:
        rc = pv.main([])
        assert rc == 1
    finally:
        wjob.DEFAULT_DUCKDB_PATH = orig_db


def test_main_smoke_mode_invokes_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--smoke --weeks 2`` pre-flight passes (stub DuckDB) and
    invokes run_smoke(2)."""
    db = tmp_path / "daily.duckdb"
    out = tmp_path / "reports"
    _populate_dummy_duckdb(db, symbol="000001", n=30)

    import ops.weekly_paper_job as wjob

    orig_db = wjob.DEFAULT_DUCKDB_PATH
    orig_out = wjob.DEFAULT_OUTPUT_DIR
    wjob.DEFAULT_DUCKDB_PATH = db
    wjob.DEFAULT_OUTPUT_DIR = out
    try:

        class _StubStrategy:
            def __init__(self) -> None:
                self._bought = False

            def on_start(self) -> None:
                return None

            def on_bar(self, bar: object) -> None:
                sym = getattr(bar, "symbol", None)
                if sym == "000001" and not self._bought:
                    self._bought = True
                    self.order_target_percent(
                        symbol="000001",
                        target_percent=0.09,
                    )

        # Strategy path passed via --strategy uses importlib; we
        # want the stub class. Skip --strategy; pass strategy_cls
        # via run_smoke instead. Test main(["--smoke", "--weeks", "1"]).
        rc = pv.main(["--smoke", "--weeks", "1"])
        assert rc == 0
        files = list(out.glob("weekly_*.json"))
        assert len(files) == 1
    finally:
        wjob.DEFAULT_DUCKDB_PATH = orig_db
        wjob.DEFAULT_OUTPUT_DIR = orig_out


# Required for test_check_dingtalk_env_* to use monkeypatch.setenv.
import pytest  # noqa: E402
