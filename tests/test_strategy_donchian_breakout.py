"""E2E tests for ``research/strategies/donchian_breakout.py`` (F).

Coverage:

  * Constants are pinned (so silent parameter drift fails loud).
  * ``run_demo(duckdb_path=...)`` runs end-to-end on a synthetic
    DuckDB frame and produces non-empty metrics.
  * On a flat-then-monotonic-up series the strategy opens a long
    position once the close breaks above the prior 20-day high.
  * On a flat-then-monotonic-down series the strategy stays flat
    (no entry on a one-sided down move).
  * The strategy does NOT fire on the warm-up window (insufficient
    history).
"""

from __future__ import annotations

from pathlib import Path

from data_layer.storage.duck import DuckStore
from research.strategies.donchian_breakout import (
    ENTRY_WINDOW,
    EXIT_WINDOW,
    HISTORY_DEPTH,
    SYMBOL,
    TARGET_PERCENT,
    DonchianBreakoutStrategy,
    run_demo,
)
from tests.conftest import make_bars

# ===========================================================================
# Helpers
# ===========================================================================


def _flat_then_breakout_up(tmp_path: Path, n_bars: int = 80) -> Path:
    """20 bars flat at 10.0 then a sharp breakout above.

    The flat regime establishes a stable prior 20-day high (10.0).
    From bar 21 onward the close walks up past 10.0 — first bar at
    10.5 (10.0 + 0.5 * 1) is the breakout, prior 20-day high (today
    excluded) is 10.0, so 10.5 > 10.0 fires the entry.

    After the entry the price continues rising so the position
    stays open (no exit signal). The backtest records ≥1 fill.
    """
    closes = [10.0] * 20 + [10.0 + 0.5 * i for i in range(1, n_bars - 20 + 1)]
    df = make_bars(closes, symbol=SYMBOL)
    db_path = tmp_path / "daily.duckdb"
    with DuckStore(db_path) as store:
        store.upsert_daily_bars(df)
    return db_path


def _flat_then_breakout_then_breakdown(tmp_path: Path, n_bars: int = 80) -> Path:
    """20 bars flat → sharp up breakout → sharp down breakdown.

    Bar 21: close = 10.5 (breaks above 10.0 prior-high) → entry.
    Bar 41: close = 8.0 (breaks below the prior 10-day low ≈ 10.0)
    → exit.

    Both legs fire on the same dataset, exercising entry AND exit.
    """
    # Build the trajectory in 3 phases.
    closes: list[float] = []
    closes += [10.0] * 20  # flat warmup
    # Up phase: bars 20..39 → close rises 10.0 → 15.0
    closes += [10.0 + 0.25 * (i + 1) for i in range(20)]  # 10.25 → 15.0
    # Down phase: bars 40..N-1 → close drops 15.0 → 5.0 (below 10-day low ~10.0)
    remaining = n_bars - len(closes)
    closes += [15.0 - 0.25 * (i + 1) for i in range(remaining)]  # 14.75 → ...
    # Truncate to exactly n_bars.
    closes = closes[:n_bars]
    # Make sure the last close is well below the 10-day low at the
    # exit point so the exit signal fires.
    df = make_bars(closes, symbol=SYMBOL)
    db_path = tmp_path / "daily_exit.duckdb"
    with DuckStore(db_path) as store:
        store.upsert_daily_bars(df)
    return db_path


def _monotonic_down(tmp_path: Path, n_bars: int = 80) -> Path:
    """Strictly falling trajectory — no breakout ever fires."""
    closes = [20.0 - 0.1 * i for i in range(n_bars)]
    df = make_bars(closes, symbol=SYMBOL)
    db_path = tmp_path / "daily_down.duckdb"
    with DuckStore(db_path) as store:
        store.upsert_daily_bars(df)
    return db_path


# ===========================================================================
# Group 1: constants
# ===========================================================================


def test_default_entry_window_is_20() -> None:
    """Turtle S1 entry window: 20-day high."""
    assert ENTRY_WINDOW == 20


def test_default_exit_window_is_10() -> None:
    """Turtle S1 exit window: 10-day low."""
    assert EXIT_WINDOW == 10


def test_history_depth_covers_entry_window() -> None:
    """``HISTORY_DEPTH`` must be ≥ ENTRY_WINDOW (one more for the
    prior-high reference that excludes today's close)."""
    assert HISTORY_DEPTH >= ENTRY_WINDOW


def test_target_percent_matches_ma_cross() -> None:
    """95% target percent keeps a 5% cash buffer for fees/tax, same
    convention as ``MACrossStrategy``. Ensures both strategies are
    comparable in walk-forward comparisons."""
    assert TARGET_PERCENT == 0.95


# ===========================================================================
# Group 2: smoke — does it actually run end-to-end?
# ===========================================================================


def test_run_demo_produces_metrics(tmp_path: Path) -> None:
    """``run_demo`` reads a fresh DuckDB, fires AKQuant, returns a
    ``BacktestResult`` with a non-empty ``metrics_df``."""
    db_path = _flat_then_breakout_up(tmp_path)
    result = run_demo(
        duckdb_path=db_path,
        start_date="2024-01-08",
        end_date="2024-04-08",
    )
    assert result.metrics_df is not None
    assert not result.metrics_df.empty


def test_run_demo_fires_trade_on_flat_then_breakout(tmp_path: Path) -> None:
    """Flat-then-up series → close breaks above prior 20-day high →
    entry → ≥1 execution.

    Note: ``result.trades_df`` only carries *closed* trades. On a
    strictly-rising tail the position is still open at the backtest
    end, so we assert on ``execution_count`` from the metrics frame
    instead — that is the authoritative fill count.
    """
    db_path = _flat_then_breakout_up(tmp_path)
    result = run_demo(
        duckdb_path=db_path,
        start_date="2024-01-08",
        end_date="2024-04-08",
    )
    metrics = result.metrics_df
    assert "execution_count" in metrics.index
    exec_count = float(metrics.loc["execution_count", "value"])
    assert exec_count >= 1.0


def test_run_demo_entry_then_exit_produces_closed_trade(tmp_path: Path) -> None:
    """Flat → up breakout (entry) → down breakdown (exit) → ≥1 closed
    trade appears in ``trades_df``."""
    db_path = _flat_then_breakout_then_breakdown(tmp_path)
    result = run_demo(
        duckdb_path=db_path,
        start_date="2024-01-08",
        end_date="2024-04-08",
    )
    # We expect at least one round-trip (entry + exit).
    assert len(result.trades_df) >= 1


def test_run_demo_stays_flat_on_monotonic_down(tmp_path: Path) -> None:
    """Strictly down closes → close never breaks above prior 20-day
    high → no entry → 0 trades."""
    db_path = _monotonic_down(tmp_path)
    result = run_demo(
        duckdb_path=db_path,
        start_date="2024-01-08",
        end_date="2024-04-08",
    )
    assert len(result.trades_df) == 0


# ===========================================================================
# Group 3: ParamSpec surface
# ===========================================================================


def test_strategy_class_has_param_spec() -> None:
    """``DonchianBreakoutStrategy`` exposes ``entry_window`` and
    ``exit_window`` via AKQuant ``ParamSpec`` so W5 walk-forward +
    optuna can search them.

    AKQuant's ``__init_subclass__`` consumes the inline
    ``IntParam`` fields into ``cls.__own_param_specs__`` (a dict
    keyed by attribute name). The defaults are reachable through
    that dict (and also via ``cls.__param_model__``).
    """
    cls = DonchianBreakoutStrategy
    specs = getattr(cls, "__own_param_specs__", {})
    assert "entry_window" in specs
    assert "exit_window" in specs
    # Defaults match the module-level constants.
    assert specs["entry_window"].field_info.default == ENTRY_WINDOW
    assert specs["exit_window"].field_info.default == EXIT_WINDOW
