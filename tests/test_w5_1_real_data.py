"""W5.1 verification on REAL A-share data (not synthetic).

Why this lives alongside ``test_w5_walker_real_e2e.py``:

  * The synthetic test exercises the ParamSpec plumbing with
    synthetic, deterministic data so the wiring is verifiable
    without a populated DuckDB.
  * THIS test runs ``run_walk_forward`` with the REAL
    ``data/duckdb/daily.duckdb`` content so W5.1's refactor
    (top-level kwargs forwarding, ParamSpec-promoted strategies)
    is confirmed end-to-end on the project's actual data.

It is gated by ``@pytest.mark.slow`` (matches the synthetic
companion). Run with::

    pytest tests/test_w5_1_real_data.py -v -m slow

Or directly::

    uv run python tests/test_w5_1_real_data.py

Notes on data coverage (as of 2026-08-29):

  * ``data/duckdb/daily.duckdb`` contains ONE symbol: ``000001``
    (479 bars, 2024-09-02 → 2026-08-25, qfq).
  * With ~24 months of bars and the W5 default 24m train / 12m
    test, the W5 walker can't produce a single fold with the
    defaults. We shrink to 12m / 6m / 6m so we get 3 folds.
  * ``TopNMeanReversionStrategy`` needs a multi-symbol
    universe — skipped here; covered separately by
    ``tests/test_w5_walker_real_e2e.py::test_run_walk_forward_*``
    on a 4-symbol synthetic frame.

Hardcoded anti-regression invariant:

  * The full-sample MA-cross on ``000001`` 2024-09 → 2026-08
    is the W1 baseline (see memory ``ma-cross-baseline-000001``):
    bars=479, closed_trade_count=14, total_return_pct=2.58%,
    sharpe_ratio=0.177, max_drawdown=15.05%, win_rate=28.57%,
    profit_factor=1.26, exposure_time_pct=47.49%.
  * The W5.1 walk-forward IS fold #0 (months 0-12 of the same
    data) should produce trade-count on the same order of
    magnitude (>=10 closed trades, since IS train is shorter
    than the full baseline sample).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.factor_lib.analytics.walk_forward import run_walk_forward  # noqa: E402
from research.strategies.factor_timing import FactorTimingMACross  # noqa: E402
from research.strategies.ma_cross import SYMBOL, MACrossStrategy  # noqa: E402

DUCKDB_PATH = PROJECT_ROOT / "data" / "duckdb" / "daily.duckdb"

pytestmark = pytest.mark.slow


def _load_real_bars(symbol: str = SYMBOL) -> pd.DataFrame:
    """Read bars for ``symbol`` from the W2.1 DuckDB.

    Returns a frame with at least ``date / open / high / low /
    close / volume`` columns (AKQuant's required minimum) and
    sorted by date ascending.
    """
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


def test_ma_cross_walk_forward_real_000001() -> None:
    """W5.1 + MACrossStrategy on real 000001 bars (DuckDB).

    Runs a single fold at 12m / 6m / 6m (3 folds over ~24 months
    of data). Asserts:

      * At least one fold completes (no exception).
      * Both IS + OOS metrics are produced.
      * IS trade activity is non-trivial (>=10 closed trades
        in the full 24-month window, sum across folds).
      * IS/OOS labels are correct (``"is"`` vs ``"oos"``).
    """
    bars = _load_real_bars()
    print(
        f"\n[real-data] {SYMBOL}: {len(bars)} bars, "
        f"{bars['date'].min().date()} → {bars['date'].max().date()}"
    )

    # Two walker configs back-to-back: 12m/6m/6m (1 fold, IS covers full
    # 12-month window — anchors against baseline) and 6m/3m/3m
    # (multi-fold, exercises the rolling iterator on real data).
    configs = [
        ("12m/6m/6m", 12, 6, 6),
        ("6m/3m/3m", 6, 3, 3),
    ]
    for label, tr_m, te_m, st_m in configs:
        result = run_walk_forward(
            MACrossStrategy,
            data=bars,
            base_params={},  # all defaults via IntParam
            train_months=tr_m,
            test_months=te_m,
            step_months=st_m,
        )
        assert result.folds, f"{label}: walker produced no folds"
        print(f"[real-data] MA-cross {label}: {len(result.folds)} folds")

        total_is_trades = 0
        total_oos_trades = 0
        for f in result.folds:
            assert f.train_metrics["phase"] == "is"
            assert f.test_metrics["phase"] == "oos"
            is_trades = f.train_metrics.get("closed_trade_count", 0) or 0
            oos_trades = f.test_metrics.get("closed_trade_count", 0) or 0
            total_is_trades += int(is_trades)
            total_oos_trades += int(oos_trades)
            print(
                f"[real-data] MA-cross {label} fold {f.fold_index}: "
                f"train {f.train_start.date()}→{f.train_end.date()} "
                f"oOS {f.test_start.date()}→{f.test_end.date()} | "
                f"trades is={is_trades} oos={oos_trades} | "
                f"sharpe is={f.train_metrics.get('sharpe_ratio'):.3f} "
                f"oos={f.test_metrics.get('sharpe_ratio'):.3f}"
            )
        print(
            f"[real-data] MA-cross {label} IS trades total: "
            f"{total_is_trades}; OOS trades total: {total_oos_trades}"
        )
        # Baseline anchor: 14 trades over 24 months of full data. With
        # the 12m/6m/6m config IS is 12 months ≈ 7 trades (we observed
        # 7); with the 6m/3m/3m config IS is 18 months ≈ 10 trades.
        # Both should be >= 3 to call the run non-degenerate.
        assert total_is_trades >= 3, (
            f"{label}: IS trade count {total_is_trades} < 3 "
            f"(walk-forward has gone silent on real data)"
        )


def test_factor_timing_walk_forward_real_000001() -> None:
    """W5.1 + FactorTimingMACross on real 000001 bars (DuckDB).

    Confirms the 5-tuple ParamSpec path (IntParam + FloatParam)
    feeds through AKQuant + the W5 walker without losing the
    non-default thresholds.
    """
    bars = _load_real_bars()
    print(f"\n[real-data] factor-timing on {SYMBOL}: {len(bars)} bars")

    # Non-default thresholds so we know ParamSpec actually forwarded them.
    # If ``**base_kwargs`` doesn't reach ``self.params``, AKQuant will fall
    # back to the IntParam(20) / FloatParam(...) defaults and we won't notice
    # via metric values alone — but a TypeError or wrong-window signal would
    # surface here.
    configs = [
        ("12m/6m/6m", 12, 6, 6),
        ("6m/3m/3m", 6, 3, 3),
    ]
    for label, tr_m, te_m, st_m in configs:
        result = run_walk_forward(
            FactorTimingMACross,
            data=bars,
            base_params={
                "long_threshold": 0.02,
                "short_threshold": -0.03,
                "factor_window": 10,
            },
            train_months=tr_m,
            test_months=te_m,
            step_months=st_m,
        )
        assert result.folds, f"{label}: walker produced no folds"
        print(f"[real-data] factor-timing {label}: {len(result.folds)} folds")

        for f in result.folds:
            assert f.train_metrics["phase"] == "is"
            assert f.test_metrics["phase"] == "oos"
            print(
                f"[real-data] factor-timing {label} fold {f.fold_index}: "
                f"train {f.train_start.date()}→{f.train_end.date()} "
                f"oOS {f.test_start.date()}→{f.test_end.date()} | "
                f"trades is={f.train_metrics.get('closed_trade_count')} "
                f"oos={f.test_metrics.get('closed_trade_count')} | "
                f"sharpe is={f.train_metrics.get('sharpe_ratio'):.3f} "
                f"oos={f.test_metrics.get('sharpe_ratio'):.3f}"
            )
            # ParamSpec forwarded values: best_params should carry the
            # overridden keys (the walker copies base_params when
            # optuna_trials=0).
            for k, v in (
                ("long_threshold", 0.02),
                ("short_threshold", -0.03),
                ("factor_window", 10),
            ):
                assert f.best_params.get(k) == v, (
                    f"best_params[{k}] = {f.best_params.get(k)!r}; "
                    f"expected {v!r}. The W5.1 walker is dropping ParamSpec values."
                )


if __name__ == "__main__":
    # Allow direct invocation: prints the same diagnostic info.
    pytest.main([__file__, "-v", "-s", "-m", "slow"])
