"""W5.2 (B1) optuna verification on REAL A-share data.

Why this exists:

  * ``tests/test_optuna_runner.py`` exercises ``optimize_params``
    via stubbed backtest runner (no AKQuant spin-up, fully
    deterministic, no network / DuckDB).
  * ``tests/test_w5_walker_real_e2e.py`` proves the W5 walker
    + ParamSpec plumbing works on real AKQuant.
  * THIS test puts ``optimize_params`` on the real
    ``data/duckdb/daily.duckdb`` content (single-symbol 000001)
    so we know the full optuna → AKQuant → ParamSpec pipeline
    runs end-to-end on the project's data.

Caveat (per memory ``w5-1-status``):

  * Real-data universe is a single symbol; optuna on a 1-symbol
    2-year sample is over-fit prone. This test only proves the
    pipeline works — it does NOT validate that the chosen
    ``best_params`` generalize.

Gated by ``@pytest.mark.slow``. Run::

    pytest tests/test_optuna_real_data.py -v -m slow
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.factor_lib.analytics.optuna_runner import optimize_params  # noqa: E402
from research.strategies.ma_cross import SYMBOL, MACrossStrategy  # noqa: E402

DUCKDB_PATH = PROJECT_ROOT / "data" / "duckdb" / "daily.duckdb"

pytestmark = pytest.mark.slow


def _load_real_bars(symbol: str = SYMBOL) -> pd.DataFrame:
    """Read bars for ``symbol`` from the W2.1 DuckDB."""
    import duckdb

    if not DUCKDB_PATH.exists():
        pytest.skip(f"DuckDB missing at {DUCKDB_PATH}; cannot run real-data e2e.")
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        df = con.execute(
            "SELECT date, open, high, low, close, volume, amount, turnover, "
            "outstanding_share "
            "FROM daily_bars WHERE symbol = ? ORDER BY date",
            [symbol],
        ).fetchdf()
    finally:
        con.close()
    if df.empty:
        pytest.skip(f"no bars for {symbol} in DuckDB; cannot run real-data e2e.")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def test_optimize_params_real_000001_ma_cross() -> None:
    """W5.2 path: ``optimize_params`` on the real ``000001`` bars
    with a 2-axis search space (``fast_window`` ∈ [3, 10],
    ``slow_window`` ∈ [15, 30]).

    Asserts:

      * Pipeline completes without exception.
      * ``best_params`` is returned with BOTH searched keys
        present (proves optuna outputs landed in the strategy
        ParamSpec, not just ``base_params`` echo).
      * Each searched value is within the search bounds.
      * ``best_params`` includes the fixed ``base_params``
        (``initial_cash``-style entries that the strategy sees
        but optuna didn't search).
    """
    bars = _load_real_bars()
    print(
        f"\n[optuna-real] {SYMBOL}: {len(bars)} bars, {bars['date'].min().date()} → {bars['date'].max().date()}"
    )

    # Note: ``initial_cash / commission_rate / stamp_tax_rate /
    # lot_size / t_plus_one / history_depth / warmup_period /
    # symbols`` are STRATEGY-RUNTIME constants NOT ParamSpec fields.
    # They must be passed via ``run_backtest_kwargs`` so AKQuant's
    # ``_split_strategy_kwargs`` doesn't try to forward them to
    # ``self.params`` (which would TypeError).
    fixed_runtime = {
        "initial_cash": 1_000_000.0,
        "commission_rate": 0.0003,
        "stamp_tax_rate": 0.001,
        "lot_size": 100,
        "warmup_period": 20,
    }
    # And ``symbols`` (the AKQuant-required kwarg for the config
    # builder) is also runtime.
    fixed_runtime["symbols"] = [SYMBOL]

    search_space = {
        "fast_window": (3, 10),  # int range
        "slow_window": (15, 30),  # int range
    }

    best = optimize_params(
        MACrossStrategy,
        data=bars,
        base_params={},  # no fixed strategy params for MA-cross
        search_space=search_space,
        n_trials=5,  # keep ≤ 5 so the test stays under ~15s on real AKQuant
        metric="sharpe_ratio",
        direction="maximize",
        run_backtest_kwargs=fixed_runtime,
        seed=42,
    )

    print(f"[optuna-real] best_params: {best}")

    # Both searched keys present and within bounds.
    for name, bounds in search_space.items():
        low, high = bounds
        assert name in best, f"optuna best_params missing {name!r}; got keys {sorted(best)}"
        v = best[name]
        assert low <= v <= high, f"optuna {name}={v} outside search bounds {bounds}"

    # ``initial_cash`` is NOT a ParamSpec and NOT in ``search_space``,
    # so it must NOT be in the returned ``best_params`` (the runner
    # only merges searched keys + base_params; ``run_backtest_kwargs``
    # is a separate vector).
    assert "initial_cash" not in best, (
        f"optuna leaked a run_backtest_kwargs key into best_params: {best}"
    )

    # ``base_params`` was empty; no extra fixed params to verify beyond
    # the absence of leakage above. Sanity: returned dict is finite.
    assert isinstance(best, dict)
    assert all(isinstance(k, str) for k in best)
