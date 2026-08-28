"""E2E tests for ``research/strategies/topn_mean_reversion.py`` (W3.2-C2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from akquant import run_backtest  # noqa: F401  # imported for sanity check
from data_layer.storage.duck import DuckStore
from research.strategies._multi_symbol_loader import load_multi_symbol_bars
from research.strategies.topn_mean_reversion import (
    DEFAULT_SYMBOLS,
    HISTORY_DEPTH,
    WINDOW,
    TopNMeanReversionStrategy,
    run_demo,
)
from tests.conftest import make_bars

# ===========================================================================
# Helpers
# ===========================================================================


def _four_symbol_universe(tmp_path: Path) -> Path:
    """Build a 4-symbol DuckDB universe with distinct trajectories.

    A: monotonically up (RSI ~100, never oversold)
    B: monotonically down (RSI ~0, deep oversold — should win rebalance)
    C: flat (RSI ~50, neutral)
    D: noisy around a flat mean (RSI fluctuates)

    ``n_bars = 80`` ensures the warm-up window (BOLL=20) clears and
    at least one Monday rebalance fires (weekly cadence on a 80-bar
    bdate_range yields ~16 Mondays).
    """
    n_bars = 80
    frames = {
        "000001": make_bars([10.0 + i * 0.05 for i in range(n_bars)], symbol="000001"),
        "600000": make_bars([20.0 - i * 0.05 for i in range(n_bars)], symbol="600000"),
        "000002": make_bars([15.0] * n_bars, symbol="000002"),
        "600519": make_bars(
            [10.0 + 0.5 * ((i % 4) - 1.5) for i in range(n_bars)],
            symbol="600519",
        ),
    }
    db_path = tmp_path / "daily.duckdb"
    with DuckStore(db_path) as store:
        for sym, df in frames.items():
            store.upsert_daily_bars(df)
    return db_path


# ===========================================================================
# Group 1: constants + symbol exposure
# ===========================================================================


def test_default_symbols_is_4() -> None:
    """The default e2e universe has exactly 4 symbols (matches the
    4-symbol helper in the test file; changing the default would
    silently drift both e2e assertions and smoke runs)."""
    assert len(DEFAULT_SYMBOLS) == 4


def test_window_covers_rsi_and_boll() -> None:
    """``WINDOW`` is the max of the RSI and Bollinger windows so a
    single ``set_history_depth`` call covers both factor inputs."""
    assert WINDOW >= 20
    assert HISTORY_DEPTH > WINDOW


# ===========================================================================
# Group 2: smoke — does it actually run end-to-end?
# ===========================================================================


def test_run_demo_produces_metrics(tmp_path: Path) -> None:
    """``run_demo`` reads a fresh DuckDB, fires AKQuant, returns a
    ``BacktestResult`` with a non-empty ``metrics_df``."""
    db_path = _four_symbol_universe(tmp_path)
    result = run_demo(duckdb_path=db_path)
    assert result.metrics_df is not None
    assert not result.metrics_df.empty


def test_run_demo_at_least_one_trade(tmp_path: Path) -> None:
    """At least one rebalance fires and produces ≥1 closed trade."""
    db_path = _four_symbol_universe(tmp_path)
    result = run_demo(duckdb_path=db_path)
    assert len(result.trades_df) >= 1


def test_run_demo_multi_symbol_trading(tmp_path: Path) -> None:
    """The cross-section strategy actually crosses symbols: ≥2
    distinct symbols appear in the trades frame. Built on a
    universe where multiple symbols go through an oversold regime
    during the run, not the mixed-trajectory default.
    """
    n_bars = 80
    # 3 of 4 symbols trend down → multiple oversold readings →
    # the cross-section strategy should hold at least 2 of them
    # at any given Monday.
    frames = {
        "000001": make_bars([20.0 - i * 0.05 for i in range(n_bars)], symbol="000001"),
        "600000": make_bars([25.0 - i * 0.07 for i in range(n_bars)], symbol="600000"),
        "000002": make_bars([18.0 - i * 0.04 for i in range(n_bars)], symbol="000002"),
        "600519": make_bars([10.0 + 0.5 * ((i % 4) - 1.5) for i in range(n_bars)], symbol="600519"),
    }
    db_path = tmp_path / "multi_oversold.duckdb"
    with DuckStore(db_path) as store:
        for sym, df in frames.items():
            store.upsert_daily_bars(df)
    result = run_demo(duckdb_path=db_path, symbols=list(frames.keys()))
    unique_symbols = result.trades_df["symbol"].unique()
    assert len(unique_symbols) >= 2


def test_run_demo_no_short_positions(tmp_path: Path) -> None:
    """``rebalance_to_topn(long_only=True)`` ⇒ all open positions
    have non-negative quantity."""
    db_path = _four_symbol_universe(tmp_path)
    result = run_demo(duckdb_path=db_path)
    if not result.positions_df.empty and "quantity" in result.positions_df.columns:
        assert (result.positions_df["quantity"] >= 0).all()


def test_run_demo_mdd_non_negative(tmp_path: Path) -> None:
    """``max_drawdown`` is reported as a positive number (the depth of
    the drawdown in pct terms — 7.21% means equity fell 7.21% from
    peak). It is bounded below by 0 (no drawdown = 0) and is
    always finite."""
    db_path = _four_symbol_universe(tmp_path)
    result = run_demo(duckdb_path=db_path)
    mdd = result.metrics_df
    if "max_drawdown" in mdd.index:
        mdd_val = float(mdd.loc["max_drawdown", "value"])
        assert 0.0 <= mdd_val <= 1.0  # AKQuant reports as fraction (0..1) or pct


# ===========================================================================
# Group 3: strategy-class wiring
# ===========================================================================


def test_strategy_class_is_subclass_of_akquant_strategy() -> None:
    """``TopNMeanReversionStrategy`` extends ``akquant.Strategy``
    (verified by class-name string match so the test does not
    depend on AKQuant's runtime types being importable)."""
    bases = [
        b.__name__ for b in TopNMeanReversionStrategy.__mro__
    ]
    assert "Strategy" in bases


def test_strategy_sets_history_depth() -> None:
    """``on_start`` sets the history depth so multi-symbol warm-up
    covers both RSI(14) and Bollinger(20). The literal ``HISTORY_DEPTH``
    symbol must appear at least once in the source (not its numeric
    value, since the strategy references it as a constant)."""
    import inspect

    src = inspect.getsource(TopNMeanReversionStrategy)
    assert "set_history_depth" in src
    assert "HISTORY_DEPTH" in src


def test_strategy_uses_rebalance_to_topn() -> None:
    """``on_cross_section`` calls ``rebalance_to_topn`` with
    ``long_only=True`` (per CLAUDE.md / design)."""
    import inspect

    src = inspect.getsource(TopNMeanReversionStrategy)
    assert "rebalance_to_topn" in src
    assert "long_only=True" in src


# ===========================================================================
# Group 4: loader + strategy integration
# ===========================================================================


def test_loader_returns_expected_symbols(tmp_path: Path) -> None:
    """``load_multi_symbol_bars`` returns the expected 4-symbol
    universe; the strategy wiring matches."""
    db_path = _four_symbol_universe(tmp_path)
    data_map = load_multi_symbol_bars(db_path, list(DEFAULT_SYMBOLS))
    assert set(data_map.keys()) == set(DEFAULT_SYMBOLS)


def test_run_demo_with_subset_symbols(tmp_path: Path) -> None:
    """A 2-symbol subset still runs (the strategy handles
    ``len(scores) < TOP_N`` defensively)."""
    db_path = _four_symbol_universe(tmp_path)
    result = run_demo(
        duckdb_path=db_path,
        symbols=("000001", "600000"),
    )
    assert not result.metrics_df.empty


# ===========================================================================
# Group 5: edge cases
# ===========================================================================


def test_run_demo_missing_duckdb_path_raises(tmp_path: Path) -> None:
    """No matching DuckDB rows ⇒ ``RuntimeError`` (not a silent
    no-trade backtest)."""
    db_path = tmp_path / "empty.duckdb"
    with pytest.raises(RuntimeError, match="No bars returned"):
        run_demo(duckdb_path=db_path)


def test_run_demo_partial_universe(tmp_path: Path) -> None:
    """Asking for a symbol not in DuckDB silently drops it from
    ``data_map``; the strategy runs on the available subset."""
    db_path = _four_symbol_universe(tmp_path)
    result = run_demo(
        duckdb_path=db_path,
        symbols=("000001", "600000", "999999"),
    )
    # 999999 is dropped; the other two run.
    assert not result.metrics_df.empty
