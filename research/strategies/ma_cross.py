"""Minimum MA-crossover strategy on AKQuant (P1 W1 backbone smoke test).

This is **not** a production strategy. It exists only to verify the
end-to-end AKQuant pipeline (akshare data → event loop → strategy →
matcher → result) for a single A-share symbol under the A-share cost /
lot / T+1 / tick-size settings required by ``CLAUDE.md``.

Strict A-share rule patches (price-limit, suspension, ex-dividend, ST
filter, delisted universe, lot-size strict enforcement, sell-only stamp
tax) are owned by the P1 W4 self-research layer and are **not**
implemented here. ``ChinaStockConfig`` here only enables ``tick_size``
enforcement, exactly as in AKQuant's stock config.
"""

from __future__ import annotations

import sys
from typing import Final

import akquant
import pandas as pd
from akquant import Bar, ChinaStockConfig, fetch_akshare_symbol, run_backtest
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

INITIAL_CASH: Final[float] = 1_000_000.0
COMMISSION_RATE: Final[float] = 0.0003
STAMP_TAX_RATE: Final[float] = 0.001
LOT_SIZE: Final[int] = 100
TARGET_PERCENT: Final[float] = 0.95  # leave 5% cash buffer for fees / tax

HISTORY_DEPTH: Final[int] = SLOW_WINDOW + FAST_WINDOW


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class MACrossStrategy(akquant.Strategy):
    """5/20 simple-MA crossover on a single symbol, near-full-cash allocation.

    On every bar:
      * Golden cross (fast crosses above slow, no position) → buy ~95% equity.
      * Death  cross (fast crosses below slow, holding)    → flatten.
      * Otherwise, hold.

    Notes:
      * Uses ``get_history_df`` + pandas rolling. We deliberately avoid the
        incremental ``Indicator`` factory path here — keep P1 W1 small,
        defer indicator-mode / warm-up complexity to P2 (factor layer).
      * The two MA values are recorded each bar via ``record_indicator``
        so a future dashboard can overlay them on the equity curve.
    """

    def on_start(self) -> None:
        # ``is_restored`` exists on Warm Start (run_from_checkpoint).
        # Guard any fresh-state init so re-runs from a checkpoint don't
        # overwrite restored state.
        try:
            restored: bool = bool(self.is_restored)
        except AttributeError:
            restored = False
        _ = restored  # placeholder for future session-state init
        self.set_history_depth(HISTORY_DEPTH)

    def on_bar(self, bar: Bar) -> None:
        # ``count`` must be ``SLOW_WINDOW + 1`` so ``iloc[-2]`` on a
        # ``rolling(SLOW_WINDOW)`` still has a full 20-bar window. With
        # ``count == SLOW_WINDOW`` the prev slot only sees 19 prior
        # bars, so ``fast_prev``/``slow_prev`` are computed over a
        # shifted-and-short window — golden/death crosses then never
        # match the offline pandas baseline and the strategy stays flat.
        df: pd.DataFrame = self.get_history_df(count=SLOW_WINDOW + 1)
        if len(df) < SLOW_WINDOW + 1:
            return  # warm-up not full yet

        closes = df["close"]
        fast_now = closes.rolling(FAST_WINDOW).mean().iloc[-1]
        slow_now = closes.rolling(SLOW_WINDOW).mean().iloc[-1]
        fast_prev = closes.rolling(FAST_WINDOW).mean().iloc[-2]
        slow_prev = closes.rolling(SLOW_WINDOW).mean().iloc[-2]

        self.record_indicator("fast_ma", float(fast_now), symbol=bar.symbol)
        self.record_indicator("slow_ma", float(slow_now), symbol=bar.symbol)

        pos_size = self.position.size

        # Golden cross → buy
        if fast_prev <= slow_prev and fast_now > slow_now and pos_size == 0:
            self.order_target_percent(
                symbol=bar.symbol,
                target_percent=TARGET_PERCENT,
            )
            return

        # Death cross → flatten
        if fast_prev >= slow_prev and fast_now < slow_now and pos_size > 0:
            self.order_target_percent(
                symbol=bar.symbol,
                target_percent=0.0,
            )


# ---------------------------------------------------------------------------
# Demo runner
# ---------------------------------------------------------------------------


def run_demo() -> BacktestResult:
    """Fetch the symbol's recent daily bars and run the MA-cross strategy.

    Returns the AKQuant ``BacktestResult`` so callers (P1 W2 data layer,
    P1 W4 A-share patch layer, P2 W5 walk-forward) can reuse this entry
    point without re-implementing wiring.
    """
    logger.info("fetching {sym} {start} → {end}", sym=SYMBOL, start=START_DATE, end=END_DATE)
    df = fetch_akshare_symbol(
        symbol=SYMBOL,
        start_date=START_DATE,
        end_date=END_DATE,
        adjust="qfq",
    )
    logger.info("fetched {n} bars", n=len(df))
    if df.empty:
        raise RuntimeError(
            f"akshare returned no rows for {SYMBOL} between "
            f"{START_DATE} and {END_DATE}; check network / token."
        )

    result = run_backtest(
        data=df,
        strategy=MACrossStrategy,
        symbols=[SYMBOL],
        initial_cash=INITIAL_CASH,
        commission_rate=COMMISSION_RATE,
        stamp_tax_rate=STAMP_TAX_RATE,
        lot_size=LOT_SIZE,
        t_plus_one=True,  # AKQuant built-in A-share T+1 switch
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
        "MA-cross done: bars={n} trades={nt} total_ret={ret:.2f}% "
        "sharpe={sh:.3f} sortino={so:.3f} mdd={dd:.2%} win_rate={wr:.2f}% "
        "profit_factor={pf:.2f} exposure={ex:.2f}% max_lev={ml:.2f}",
        n=len(df),
        nt=len(result.trades_df),
        ret=_row("total_return_pct"),
        sh=_row("sharpe_ratio"),
        so=_row("sortino_ratio"),
        dd=_row("max_drawdown"),
        wr=_row("win_rate"),
        pf=_row("profit_factor"),
        ex=_row("exposure_time_pct"),
        ml=_row("max_leverage"),
    )
    return result


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO", colorize=True)
    run_demo()
