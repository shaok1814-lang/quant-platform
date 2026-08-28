"""Single-symbol factor-timing MA-cross strategy on AKQuant (W3.2-C3).

The same 5/20 SMA crossover as ``ma_cross.py``, but with a
momentum factor (n-day return over ``FACTOR_WINDOW``) acting as a
**gate**:

  * Buy only when the golden cross fires AND the n-day return is
    positive (i.e. we only join a momentum that already exists).
  * Exit on death cross OR on a deep-negative n-day return
    (``SHORT_THRESHOLD``) — i.e. if momentum turns sharply negative,
    flatten immediately rather than wait for the slow cross to
    drag below fast.

This is the "factor as filter / timing aid" pattern: the factor
does not generate the trade idea on its own — it modulates when
the MA-cross fires. W3.2 ships this single-symbol demo so the
factor-as-filter shape is exercised end-to-end before W5 wires it
into the cross-section rank.

Per CLAUDE.md the strict A-share patch layer (price-limit,
suspension, ST filter, delisted universe, strict lot enforcement,
sell-only stamp tax) is owned by P1 W4 and is NOT implemented here.
``ChinaStockConfig`` below only enables ``tick_size`` enforcement.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

import akquant
import pandas as pd
from akquant import Bar, ChinaStockConfig, run_backtest
from akquant.backtest.result import BacktestResult
from akquant.config import (
    BacktestConfig,
    InstrumentConfig,
    RiskConfig,
    StrategyConfig,
)
from loguru import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYMBOL: Final[str] = "000001"
START_DATE: Final[str] = "20240901"
END_DATE: Final[str] = "20260825"

FAST_WINDOW: Final[int] = 5
SLOW_WINDOW: Final[int] = 20
FACTOR_WINDOW: Final[int] = 20

INITIAL_CASH: Final[float] = 1_000_000.0
COMMISSION_RATE: Final[float] = 0.0003
STAMP_TAX_RATE: Final[float] = 0.001
LOT_SIZE: Final[int] = 100
TARGET_PERCENT: Final[float] = 0.95  # leave 5% cash buffer for fees / tax

LONG_THRESHOLD: Final[float] = 0.0  # require positive momentum to open
SHORT_THRESHOLD: Final[float] = -0.05  # deep negative → flatten without
# waiting for the slow cross

HISTORY_DEPTH: Final[int] = max(SLOW_WINDOW, FACTOR_WINDOW) + 2

# ---------------------------------------------------------------------------
# sys.path bootstrap (mirrors ma_cross_duckdb.py / topn_mean_reversion.py)
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from research.factor_lib.momentum import n_day_return  # noqa: E402

__all__ = [
    "COMMISSION_RATE",
    "END_DATE",
    "FACTOR_WINDOW",
    "FAST_WINDOW",
    "HISTORY_DEPTH",
    "INITIAL_CASH",
    "LONG_THRESHOLD",
    "LOT_SIZE",
    "SHORT_THRESHOLD",
    "SLOW_WINDOW",
    "STAMP_TAX_RATE",
    "START_DATE",
    "SYMBOL",
    "TARGET_PERCENT",
    "FactorTimingMACross",
    "run_demo",
]


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class FactorTimingMACross(akquant.Strategy):  # type: ignore[misc]
    """MA-cross gated by a momentum factor.

    Decision logic per bar:

      * Golden cross AND ``nret > LONG_THRESHOLD`` AND flat → buy
        near-full equity.
      * Death cross AND holding → flatten.
      * Holding AND ``nret < SHORT_THRESHOLD`` → flatten
        immediately (defensive override; do not wait for the slow
        cross).

    Indicators ``fast_ma`` / ``slow_ma`` / ``nret_{FACTOR_WINDOW}``
    are recorded every bar so a future dashboard can overlay them
    on the equity curve.
    """

    def on_start(self) -> None:
        try:
            restored: bool = bool(self.is_restored)
        except AttributeError:
            restored = False
        _ = restored
        self.set_history_depth(HISTORY_DEPTH)

    def on_bar(self, bar: Bar) -> None:
        df: pd.DataFrame = self.get_history_df(count=SLOW_WINDOW + 1)
        if len(df) < SLOW_WINDOW + 1:
            return  # warm-up not full yet
        closes = df["close"]
        fast_now = closes.rolling(FAST_WINDOW).mean().iloc[-1]
        slow_now = closes.rolling(SLOW_WINDOW).mean().iloc[-1]
        fast_prev = closes.rolling(FAST_WINDOW).mean().iloc[-2]
        slow_prev = closes.rolling(SLOW_WINDOW).mean().iloc[-2]
        # Momentum factor over FACTOR_WINDOW bars; uses the latest
        # bar's close in the numerator, so for a defensive
        # end-of-bar decision the strategy should be run with
        # close.shift(1) upstream — not done here because the
        # MA-cross template is consistent with ma_cross.py.
        nret_series = n_day_return(closes, window=FACTOR_WINDOW)
        nret_now = nret_series.iloc[-1]
        if pd.isna(nret_now):
            return

        self.record_indicator("fast_ma", float(fast_now), symbol=bar.symbol)
        self.record_indicator("slow_ma", float(slow_now), symbol=bar.symbol)
        self.record_indicator(f"nret_{FACTOR_WINDOW}", float(nret_now), symbol=bar.symbol)

        pos_size = self.position.size

        # Golden cross AND positive momentum → buy.
        if (
            fast_prev <= slow_prev
            and fast_now > slow_now
            and pos_size == 0
            and float(nret_now) > LONG_THRESHOLD
        ):
            self.order_target_percent(
                symbol=bar.symbol,
                target_percent=TARGET_PERCENT,
            )
            return

        # Death cross OR defensive momentum-stop → flatten.
        if pos_size > 0 and (
            (fast_prev >= slow_prev and fast_now < slow_now) or float(nret_now) < SHORT_THRESHOLD
        ):
            self.order_target_percent(
                symbol=bar.symbol,
                target_percent=0.0,
            )


# ---------------------------------------------------------------------------
# Demo runner
# ---------------------------------------------------------------------------


def run_demo(
    duckdb_path: str | Path | None = None,
    start_date: str = START_DATE,
    end_date: str = END_DATE,
) -> BacktestResult:
    """Run the factor-timing strategy end-to-end.

    Args:
        duckdb_path: If provided, read bars from DuckDB via
            ``DuckStore`` (the W3.2 path). If ``None``, fall back
            to akshare-direct (the W1.1 path) so the strategy is
            runnable without a populated DuckDB.
        start_date: ISO start (YYYY-MM-DD for DuckDB /
            YYYYMMDD for akshare — both accepted by their
            respective fetchers).
        end_date: ISO end.

    Returns:
        The ``BacktestResult`` so callers (W5 walk-forward, P3
        dashboard) can reuse this entry point.
    """
    if duckdb_path is not None:
        from data_layer.storage.duck import DuckStore

        with DuckStore(duckdb_path) as store:
            df = store.query_daily_bars(SYMBOL, start_date=start_date, end_date=end_date)
        if df.empty:
            raise RuntimeError(
                f"No bars returned from DuckDB at {duckdb_path} for {SYMBOL} "
                f"between {start_date} and {end_date}."
            )
    else:
        from akquant import fetch_akshare_symbol

        logger.info("fetching {sym} {start} -> {end}", sym=SYMBOL, start=start_date, end=end_date)
        # akshare uses YYYYMMDD; convert ISO if needed.
        ak_start = start_date.replace("-", "")
        ak_end = end_date.replace("-", "")
        df = fetch_akshare_symbol(
            symbol=SYMBOL,
            start_date=ak_start,
            end_date=ak_end,
            adjust="qfq",
        )
        if df.empty:
            raise RuntimeError(
                f"akshare returned no rows for {SYMBOL} between {start_date} "
                f"and {end_date}; check network / token."
            )
    logger.info("fetched {n} bars", n=len(df))

    result = run_backtest(
        data=df,
        strategy=FactorTimingMACross,
        symbols=[SYMBOL],
        initial_cash=INITIAL_CASH,
        commission_rate=COMMISSION_RATE,
        stamp_tax_rate=STAMP_TAX_RATE,
        lot_size=LOT_SIZE,
        t_plus_one=True,
        history_depth=HISTORY_DEPTH,
        warmup_period=SLOW_WINDOW,
        config=BacktestConfig(
            strategy_config=StrategyConfig(
                initial_cash=INITIAL_CASH,
                risk=RiskConfig(max_position_pct=TARGET_PERCENT),
            ),
            instruments_config=[
                InstrumentConfig(
                    symbol=SYMBOL,
                    asset_type="STOCK",
                    tick_size=0.01,
                    lot_size=LOT_SIZE,
                ),
            ],
            china_stock=ChinaStockConfig(enforce_tick_size=True),
            show_progress=False,
        ),
    )

    metrics = result.metrics_df
    if metrics.empty:
        logger.warning("metrics_df is empty; cannot print summary")
        return result

    def _row(name: str) -> float:
        try:
            return float(metrics.loc[name, "value"])
        except (KeyError, TypeError, ValueError):
            return float("nan")

    logger.success(
        "Factor-timing MA-cross done: bars={n} trades={nt} total_ret={ret:.2f}% "
        "sharpe={sh:.3f} sortino={so:.3f} mdd={dd:.2%} win_rate={wr:.2f}%",
        n=len(df),
        nt=len(result.trades_df),
        ret=_row("total_return_pct"),
        sh=_row("sharpe_ratio"),
        so=_row("sortino_ratio"),
        dd=_row("max_drawdown"),
        wr=_row("win_rate"),
    )
    return result


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO", colorize=True)
    run_demo()
