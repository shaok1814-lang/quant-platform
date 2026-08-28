"""E2E tests for ``research/strategies/factor_timing.py`` (W3.2-C3)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from data_layer.storage.duck import DuckStore
from research.strategies.factor_timing import (
    FACTOR_WINDOW,
    HISTORY_DEPTH,
    LONG_THRESHOLD,
    SLOW_WINDOW,
    FactorTimingMACross,
    run_demo,
)
from tests.conftest import make_bars


def _flat_then_up_symbol(tmp_path: Path, n_bars: int = 60) -> Path:
    """Build a single-symbol DuckDB frame: 20 bars flat at 10.0,
    then 40 bars rising linearly.

    The flat-then-up shape guarantees a real golden cross at
    bar 21: during the flat regime both fast (5-MA) and slow
    (20-MA) sit at 10.0 (so ``fast_prev <= slow_prev``); once
    prices start rising the fast  reacts first, so on the very
    next bar ``fast_now > slow_now`` and the cross fires. A
    strictly-monotonic-up series would NOT fire (fast > slow
    permanently, so ``fast_prev <= slow_prev`` never holds) —
    which is why this test deliberately avoids the pure
    monotonic trajectory.
    """
    closes = [10.0] * 20 + [10.0 + 0.5 * i for i in range(n_bars - 20)]
    df = make_bars(closes, symbol="000001")
    db_path = tmp_path / "daily.duckdb"
    with DuckStore(db_path) as store:
        store.upsert_daily_bars(df)
    return db_path


def _monotonic_down_symbol(tmp_path: Path, n_bars: int = 60) -> Path:
    """Build a single-symbol DuckDB frame with monotonically falling closes.

    The defensive SHORT_THRESHOLD override should fire and flatten
    any position before the death cross has time to land — but with
    the strict monotonic-down trajectory, the strategy never opens
    in the first place because the golden cross never fires. We
    use it here to assert the strategy stays flat (no false
    trades on a strictly-down trend).
    """
    closes = [20.0 - i * 0.1 for i in range(n_bars)]
    df = make_bars(closes, symbol="000001")
    db_path = tmp_path / "daily_down.duckdb"
    with DuckStore(db_path) as store:
        store.upsert_daily_bars(df)
    return db_path


# ===========================================================================
# Group 1: constants
# ===========================================================================


def test_long_threshold_is_zero() -> None:
    """Default LONG_THRESHOLD = 0.0: any positive momentum enables
    entry on a golden cross."""
    assert LONG_THRESHOLD == 0.0


def test_history_depth_covers_slow_window() -> None:
    """HISTORY_DEPTH is sized for the slow MA + a one-bar look-back."""
    assert HISTORY_DEPTH > SLOW_WINDOW
    assert HISTORY_DEPTH >= max(SLOW_WINDOW, FACTOR_WINDOW) + 2


# ===========================================================================
# Group 2: smoke — does it actually run?
# ===========================================================================


def test_run_demo_produces_metrics(tmp_path: Path) -> None:
    db_path = _flat_then_up_symbol(tmp_path)
    result = run_demo(
        duckdb_path=db_path,
        start_date="2024-01-08",
        end_date="2024-04-08",
    )
    assert result.metrics_df is not None
    assert not result.metrics_df.empty


def test_run_demo_fires_trade_on_flat_then_up(tmp_path: Path) -> None:
    """Flat-then-up closes produce a real golden cross at bar 21;
    combined with positive momentum the strategy opens and the
    backtest records ≥1 execution (buy fill).

    Note: ``result.trades_df`` only carries *closed* trades. On
    a strictly-rising tail the position is still open at the
    backtest end, so we assert on ``execution_count`` from the
    metrics frame instead — that is the authoritative fill count.
    """
    db_path = _flat_then_up_symbol(tmp_path)
    result = run_demo(
        duckdb_path=db_path,
        start_date="2024-01-08",
        end_date="2024-04-08",
    )
    metrics = result.metrics_df
    assert "execution_count" in metrics.index
    exec_count = float(metrics.loc["execution_count", "value"])
    assert exec_count >= 1.0


def test_run_demo_stays_flat_on_monotonic_down(tmp_path: Path) -> None:
    """Strictly down closes ⇒ no golden cross ⇒ no entries ⇒ 0
    trades. The defensive SHORT_THRESHOLD override is a
    no-op here because no position ever opens.
    """
    db_path = _monotonic_down_symbol(tmp_path)
    result = run_demo(
        duckdb_path=db_path,
        start_date="2024-01-08",
        end_date="2024-04-08",
    )
    # No golden cross on a monotonic-down series.
    assert len(result.trades_df) == 0


def test_run_demo_metrics_finite(tmp_path: Path) -> None:
    """All numeric reported metrics are finite (no NaN / inf from
    the factor-timing branch). Filter to numeric rows because
    AKQuant's ``metrics_df`` may carry Timestamp entries (start /
    end time) that would fail ``abs()``.
    """
    db_path = _flat_then_up_symbol(tmp_path)
    result = run_demo(
        duckdb_path=db_path,
        start_date="2024-01-08",
        end_date="2024-04-08",
    )
    metrics = result.metrics_df
    numeric_mask = metrics["value"].apply(lambda x: isinstance(x, (int, float)))
    numeric_metrics = metrics.loc[numeric_mask, "value"]
    finite_mask = numeric_metrics.apply(lambda x: pd.notna(x) and abs(x) < 1e12)
    assert finite_mask.all()


# ===========================================================================
# Group 3: wiring
# ===========================================================================


def test_strategy_class_is_subclass_of_akquant_strategy() -> None:
    """``FactorTimingMACross`` extends ``akquant.Strategy``."""
    bases = [b.__name__ for b in FactorTimingMACross.__mro__]
    assert "Strategy" in bases


def test_strategy_records_three_indicators() -> None:
    """``on_bar`` records ``fast_ma``, ``slow_ma``, and the momentum
    factor so a future dashboard can overlay them on the equity
    curve."""
    import inspect

    src = inspect.getsource(FactorTimingMACross)
    assert "record_indicator" in src
    assert '"fast_ma"' in src
    assert '"slow_ma"' in src
    assert "nret_" in src


def test_strategy_uses_n_day_return() -> None:
    """``on_bar`` calls the factor library's ``n_day_return`` to gate
    the MA-cross."""
    import inspect

    src = inspect.getsource(FactorTimingMACross)
    assert "n_day_return" in src
    assert "FACTOR_WINDOW" in src


# ===========================================================================
# Group 4: edge cases
# ===========================================================================


def test_run_demo_empty_duckdb_raises(tmp_path: Path) -> None:
    """No rows in DuckDB ⇒ ``RuntimeError`` (not a silent empty backtest)."""
    db_path = tmp_path / "empty.duckdb"
    with pytest.raises(RuntimeError, match="No bars returned"):
        run_demo(
            duckdb_path=db_path,
            start_date="2024-01-08",
            end_date="2024-04-08",
        )
