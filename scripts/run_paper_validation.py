"""Pre-flight checker for the 4-week paper-validation launch (W7.1 + W6.5).

Enforces CLAUDE.md 「实盘前必须经过至少 4 周模拟盘验证」 pre-conditions
before the operator kicks off ``python -m ops`` for the long-running
validation cycle.

The script is NOT the production scheduler — that lives in
``python -m ops`` (:mod:`ops.__main__`) and runs the daily ingest +
weekly paper cron. This script is a one-shot pre-flight that the
operator runs once before launching, then exits.

Checks (each prints OK / WARN / FAIL):

  1. DuckDB exists at ``data/duckdb/daily.duckdb``.
  2. DuckDB has ≥ :data:`ops.weekly_paper_job.DEFAULT_LOOKBACK_DAYS`
     calendar days of OHLCV for the default symbol (default
     ``"000001"``).
  3. ``DINGTALK_WEBHOOK_URL`` env var set (kill-switch alerts are
     best-effort without it, but the operator should opt in).
  4. ``data/paper_reports/`` is writable.
  5. AKQuant is importable (the bridge's strategy is AKQuant-only).
  6. :func:`ops.scheduler.build_scheduler` builds cleanly (catches
     config typos before the cron launches).

Modes:

  ``python scripts/run_paper_validation.py``     pre-flight only (default)
  ``python scripts/run_paper_validation.py --smoke``   pre-flight + 1 weekly run
  ``python scripts/run_paper_validation.py --smoke --weeks N``  pre-flight + N weekly runs

Exit code: ``0`` if all FAILs are 0; ``1`` otherwise.

Why a separate CLI rather than baking into ``python -m ops``:
the scheduler is a blocking main loop. We want a quick YES/NO
before committing to the loop, especially on Windows where
``task scheduler`` is the typical deployment target.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Final

_PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent


def _print(level: str, msg: str) -> None:
    """Print a pre-flight line. Levels: OK / WARN / FAIL / INFO."""
    print(f"  [{level}] {msg}")


def _section(name: str) -> None:
    print(f"\n--- {name} ---")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_duckdb_exists() -> bool:
    from ops.weekly_paper_job import DEFAULT_DUCKDB_PATH

    if DEFAULT_DUCKDB_PATH.exists():
        _print("OK", f"DuckDB present at {DEFAULT_DUCKDB_PATH}")
        return True
    _print(
        "FAIL",
        f"DuckDB missing at {DEFAULT_DUCKDB_PATH}. Run "
        f"``ops.ingest_job.run_daily_ingest`` first to populate it.",
    )
    return False


def check_duckdb_data(symbol: str) -> bool:
    """DuckDB has ≥ lookback_days of OHLCV for ``symbol``."""
    from data_layer.storage.duck import DuckStore
    from ops.weekly_paper_job import DEFAULT_DUCKDB_PATH, DEFAULT_LOOKBACK_DAYS

    if not DEFAULT_DUCKDB_PATH.exists():
        return False
    today = _today_iso()
    lookback_start = _today_minus_days(DEFAULT_LOOKBACK_DAYS)
    try:
        with DuckStore(DEFAULT_DUCKDB_PATH) as store:
            df = store.query_daily_bars(
                symbol=symbol,
                start_date=lookback_start,
                end_date=today,
            )
    except Exception as exc:  # pragma: no cover -- DuckDB read errors
        _print("FAIL", f"DuckDB read for {symbol} raised: {exc}")
        return False
    n_rows = len(df)
    if n_rows < 30:  # arbitrary low bound; the weekly paper job
        # itself raises ValueError if zero rows.
        _print(
            "FAIL",
            f"DuckDB has only {n_rows} rows for {symbol} in the "
            f"last {DEFAULT_LOOKBACK_DAYS} days. Run "
            f"``ops.ingest_job.ingest_window`` to backfill.",
        )
        return False
    _print("OK", f"DuckDB has {n_rows} rows for {symbol} in the lookback window")
    return True


def check_dingtalk_env() -> bool:
    """``DINGTALK_WEBHOOK_URL`` env var set."""
    url = os.environ.get("DINGTALK_WEBHOOK_URL")
    if url:
        _print("OK", "DINGTALK_WEBHOOK_URL is configured")
        return True
    _print(
        "WARN",
        "DINGTALK_WEBHOOK_URL not set — kill-switch alerts will be "
        "logged at ERROR but NOT delivered to 钉聊. The weekly paper "
        "session still runs; alerts are best-effort.",
    )
    return False  # WARN, not FAIL — pre-flight stays green.


def check_output_dir_writable() -> bool:
    from ops.weekly_paper_job import DEFAULT_OUTPUT_DIR

    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    probe = DEFAULT_OUTPUT_DIR / ".preflight_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        _print("FAIL", f"cannot write to {DEFAULT_OUTPUT_DIR}: {exc}")
        return False
    _print("OK", f"{DEFAULT_OUTPUT_DIR} is writable")
    return True


def check_akquant_importable() -> bool:
    try:
        import akquant  # noqa: F401
    except ImportError as exc:
        _print("FAIL", f"akquant not importable: {exc}")
        return False
    _print("OK", "akquant importable")
    return True


def check_scheduler_build() -> bool:
    from ops.scheduler import build_scheduler

    try:
        scheduler = build_scheduler()
    except Exception as exc:
        _print("FAIL", f"build_scheduler raised: {exc}")
        return False
    n_jobs = len(scheduler.get_jobs())
    if n_jobs < 2:
        _print(
            "FAIL",
            f"scheduler has {n_jobs} job(s); expected 2 "
            f"(daily_ingest + weekly_paper). Check "
            f"OPS_WEEKLY_PAPER_ENABLED.",
        )
        return False
    _print("OK", f"scheduler built with {n_jobs} jobs (daily_ingest + weekly_paper)")
    return True


# ---------------------------------------------------------------------------
# Date helpers (avoid importing datetime on the success path)
# ---------------------------------------------------------------------------


def _today_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).date().isoformat()


def _today_minus_days(days: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC).date() - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# Smoke mode
# ---------------------------------------------------------------------------


def run_smoke(n_weeks: int, *, strategy_cls: type | None = None) -> int:
    """Run the weekly paper job N times in sequence.

    Each run uses the production DuckDB (``DEFAULT_DUCKDB_PATH``)
    + production output dir (``DEFAULT_OUTPUT_DIR``) — i.e. it
    actually writes JSON to ``data/paper_reports/``. Designed for
    an operator to verify the full path before launching the
    scheduler.

    The N runs reuse the same ``report_path`` (per-rotation SQLite
    journal name has the date baked in) so each run OVERWRITES the
    previous one. The smoke is meant as a one-shot warm-up, not a
    4-week simulation. For 4 weeks, run the actual scheduler.
    """
    from ops.weekly_paper_job import run_weekly_paper_session

    print(
        f"\n--- smoke: running {n_weeks} weekly paper session(s) ---\n"
        "    (overwrites the same report_path each time)\n"
    )
    failures = 0
    for i in range(1, n_weeks + 1):
        print(f"[smoke {i}/{n_weeks}] running...")
        try:
            weekly = run_weekly_paper_session(
                strategy_cls=strategy_cls,
                notify_on_kill_switch=False,
            )
            print(
                f"  [smoke {i}/{n_weeks}] wrote {weekly.report_path} "
                f"(fills={weekly.n_filled}, equity={weekly.final_equity:.0f})"
            )
        except Exception as exc:
            failures += 1
            print(f"  [smoke {i}/{n_weeks}] FAILED: {exc}")
    if failures:
        print(f"\n{failures} of {n_weeks} weekly runs failed")
        return 1
    print(f"\nAll {n_weeks} weekly run(s) completed.")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pre-flight check + optional smoke for 4-week paper validation.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="After pre-flight, run N weekly paper sessions (default 1).",
    )
    parser.add_argument(
        "--weeks",
        type=int,
        default=1,
        help="Number of weekly runs in smoke mode (default 1).",
    )
    parser.add_argument(
        "--strategy",
        default=None,
        help=(
            "Override the default MACrossStrategy. Pass a fully-qualified "
            "class path like 'research.strategies.factor_timing.FactorTimingMACross'."
            " Used by tests / custom validation setups; production keeps "
            "the W1 baseline."
        ),
    )
    args = parser.parse_args(argv)

    print("=" * 60)
    print("Paper-validation pre-flight (CLAUDE.md 4-week requirement)")
    print("=" * 60)

    fails = 0
    fails += 0 if check_duckdb_exists() else 1
    fails += 0 if check_duckdb_data(DEFAULT_SYMBOL) else 1
    # 钉聊 check is WARN-only — missing webhook degrades alerts but the
    # pre-flight stays green. Add the count separately so the
    # summary at the end can surface "X warnings".
    warnings = 0 if check_dingtalk_env() else 1
    fails += 0 if check_output_dir_writable() else 1
    fails += 0 if check_akquant_importable() else 1
    fails += 0 if check_scheduler_build() else 1

    print("\n" + "=" * 60)
    if fails:
        print(f"PRE-FLIGHT FAILED ({fails} blocker(s)). Fix above and retry.")
        return 1
    if warnings:
        print(f"PRE-FLIGHT PASSED with {warnings} warning(s). See above.")
    else:
        print("PRE-FLIGHT PASSED.")
    print("")
    print("To start the 4-week validation cycle:")
    print("  python -m ops")
    print("")
    print("To monitor:")
    print("  streamlit run ops/dashboard.py   # see 'Paper Trade History' page")
    print("  ls data/paper_reports/             # weekly JSON + journal files")
    print("")

    if args.smoke:
        strategy_cls = _load_strategy_class(args.strategy) if args.strategy else None
        return run_smoke(args.weeks, strategy_cls=strategy_cls)
    return 0


def _load_strategy_class(path: str) -> type:
    """Resolve a fully-qualified class path into a class object.

    ``module.path.ClassName`` → ``importlib.import_module('module.path').ClassName``.
    Used by ``--strategy``.
    """
    import importlib

    module_path, _, attr = path.rpartition(".")
    if not module_path:
        raise ValueError(f"--strategy must be a fully-qualified path; got {path!r}")
    cls: type = getattr(importlib.import_module(module_path), attr)
    return cls


DEFAULT_SYMBOL = "000001"


if __name__ == "__main__":
    sys.path.insert(0, str(_PROJECT_ROOT))
    sys.exit(main())
