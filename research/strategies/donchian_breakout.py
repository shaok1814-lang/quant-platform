"""Donchian Channel Breakout strategy (Turtle System 1 simplified, W7.1+F).

This is a **production-grade** trend-following strategy, distinct
from MA-cross (which is a moving-average-confirmation system).
Donchian breakouts follow Michael Marcus / Richard Donchian's
"trade with the trend" principle: enter on strength (close breaks
above N-day high), exit on weakness (close breaks below M-day low).

Per-bar logic:
  * Close > entry_window-day high, no position → buy 95% equity.
  * Close < exit_window-day low, holding        → flatten (sell).
  * Otherwise, hold.

The classic Turtle System 1 uses ``entry_window=20``,
``exit_window=10``. Both are AKQuant ``IntParam`` so they participate
in walk-forward / optuna search (W5).

Single-symbol (same convention as ``MACrossStrategy``). Tested
with the AKQuant bridge in ``execution/bridge/akquant_strategy.py``;
``get_history_df(count=)`` + ``record_indicator`` + ``position.size``
all work via the bridge's per-symbol routing (W7.1 Phase 4).

The strategy is INTENTIONALLY simple — no ATR sizing, no
pyramiding, no time stops. The 20/10 windows are the original
Turtle defaults and have ~40 years of out-of-sample evidence
across equity indices and futures. Adding ATR sizing or
pyramiding belongs to a follow-up strategy (e.g. ``TurtleS2``).

**NOT IN SCOPE here** (Phase 5+):
  * ATR-based position sizing (use RiskConfig's 10% cap for now).
  * Pyramiding (multiple entries on successive breakouts).
  * Time-based stops (the M-day low exit already covers trend
    failure).
  * Multi-symbol (this file is single-symbol; use
    ``topn_mean_reversion`` for the basket variant).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

import akquant
import pandas as pd
from akquant import Bar, run_backtest
from akquant.backtest.result import BacktestResult
from akquant.params import IntParam
from loguru import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYMBOL: Final[str] = "000001"
START_DATE: Final[str] = "20240901"
END_DATE: Final[str] = "20260825"

# Classic Turtle System 1 defaults (Donchian, 1970s).
ENTRY_WINDOW: Final[int] = 20
EXIT_WINDOW: Final[int] = 10

INITIAL_CASH: Final[float] = 1_000_000.0
COMMISSION_RATE: Final[float] = 0.0003
STAMP_TAX_RATE: Final[float] = 0.001
LOT_SIZE: Final[int] = 100
TARGET_PERCENT: Final[float] = 0.95  # leave 5% cash buffer for fees / tax

HISTORY_DEPTH: Final[int] = ENTRY_WINDOW + 1


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class DonchianBreakoutStrategy(akquant.Strategy):
    """Donchian channel breakout (Turtle System 1) on a single symbol.

    Tunable parameters (W5 walk-forward / optuna):
      * ``entry_window`` (int, default 20) — lookback for the
        N-day high (entry breakout).
      * ``exit_window`` (int, default 10) — lookback for the
        M-day low (exit breakdown).

    On every bar:
      * Close > entry_window-day high (no position) → buy 95%.
      * Close < exit_window-day low (holding)       → flatten.
      * Otherwise, hold.

    Notes:
      * Uses ``get_history_df`` + pandas rolling max/min. We use
        ``close.shift(1)`` so the current bar's close is
        EXCLUDED from the channel — today's breakout is a fresh
        signal, not a comparison with itself. Without the shift,
        the channel includes today's close, and any close above
        the prior N-day high trivially becomes a "breakout" — a
        degenerate strategy that holds forever.
      * Records ``donchian_high`` / ``donchian_low`` indicators
        each bar so the dashboard can overlay the channel.
    """

    # Inline ParamSpec fields. AKQuant's ``__init_subclass__`` collects
    # these into a frozen pydantic ``ParamModel`` accessible via
    # ``self.params.<name>`` (the class attribute is ``delattr``'d).
    entry_window: int = IntParam(ENTRY_WINDOW)  # type: ignore[assignment]
    exit_window: int = IntParam(EXIT_WINDOW)  # type: ignore[assignment]

    def on_start(self) -> None:
        try:
            restored: bool = bool(self.is_restored)
        except AttributeError:
            restored = False
        _ = restored
        self.set_history_depth(HISTORY_DEPTH)

    def on_bar(self, bar: Bar) -> None:
        # Need at least entry_window bars of history (the prior
        # N days, EXCLUDING today's close, is the reference high).
        # ``count=entry_window`` gives us exactly N-1 prior bars +
        # today's bar (the one we're processing); ``shift(1)`` on
        # ``close`` drops today's close from the channel.
        df: pd.DataFrame = self.get_history_df(count=self.params.entry_window)
        if len(df) < self.params.entry_window:
            return  # warm-up not full yet

        closes = df["close"]
        prior_high = closes.iloc[:-1].max()  # today's close NOT included
        prior_low_for_exit = (
            df["low"]
            .iloc[:-1]
            .tail(
                self.params.exit_window,
            )
            .min()
            if len(df) > self.params.exit_window
            else closes.iloc[:-1].min()
        )

        self.record_indicator("donchian_high", float(prior_high), symbol=bar.symbol)
        self.record_indicator(
            "donchian_low",
            float(prior_low_for_exit),
            symbol=bar.symbol,
        )

        pos_size = self.position.size
        current_close = float(bar.close)

        # Entry: today's close breaks above the prior N-day high.
        if current_close > prior_high and pos_size == 0:
            self.order_target_percent(
                symbol=bar.symbol,
                target_percent=TARGET_PERCENT,
            )
            return

        # Exit: today's close breaks below the prior M-day low.
        if current_close < prior_low_for_exit and pos_size > 0:
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
    """Run the Donchian breakout strategy end-to-end.

    Args:
        duckdb_path: If provided, read bars from DuckDB via
            ``DuckStore`` (the W3.2 path). If ``None``, fall back
            to akshare-direct (the W1.1 path) so the strategy is
            runnable without a populated DuckDB.
        start_date: ISO start (YYYY-MM-DD for DuckDB /
            YYYYMMDD for akshare — both accepted).
        end_date: ISO end.

    Returns:
        The ``BacktestResult`` so callers (W5 walk-forward, P3
        dashboard) can reuse this entry point.
    """
    if duckdb_path is not None:
        from data_layer.storage.duck import DuckStore

        with DuckStore(duckdb_path) as store:
            df = store.query_daily_bars(
                SYMBOL,
                start_date=start_date,
                end_date=end_date,
            )
        if df.empty:
            raise RuntimeError(
                f"No bars returned from DuckDB at {duckdb_path} for "
                f"{SYMBOL} between {start_date} and {end_date}."
            )
    else:
        from akquant import fetch_akshare_symbol

        logger.info(
            "fetching {sym} {start} -> {end}",
            sym=SYMBOL,
            start=start_date,
            end=end_date,
        )
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
                f"akshare returned no rows for {SYMBOL} between "
                f"{start_date} and {end_date}; check network / token."
            )
    logger.info("fetched {n} bars", n=len(df))

    result = run_backtest(
        data=df,
        strategy=DonchianBreakoutStrategy,
        symbols=[SYMBOL],
        initial_cash=INITIAL_CASH,
        commission_rate=COMMISSION_RATE,
        stamp_tax_rate=STAMP_TAX_RATE,
        lot_size=LOT_SIZE,
        t_plus_one=True,
        history_depth=HISTORY_DEPTH,
        warmup_period=ENTRY_WINDOW,
        entry_window=ENTRY_WINDOW,
        exit_window=EXIT_WINDOW,
    )
    logger.info(
        "Donchian breakout demo done symbol={sym} bars={n} entry_window={ew} exit_window={xw}",
        sym=SYMBOL,
        n=len(df),
        ew=ENTRY_WINDOW,
        xw=EXIT_WINDOW,
    )
    return result


if __name__ == "__main__":  # pragma: no cover
    sys.exit(0 if run_demo() is not None else 1)
