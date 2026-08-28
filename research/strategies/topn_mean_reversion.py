"""Top-N mean-reversion cross-section strategy on AKQuant (W3.2-C2,
refactored W5.1 to expose tunable params via AKQuant ParamSpec).

Picks the N symbols whose RSI + Bollinger z signal the strongest
"oversold" reading (low RSI = mean-reversion trigger from below;
negative Bollinger z = price below recent mean), then holds them
equal-weight on a weekly rebalance cadence. The bet is a textbook
A-share retail-style mean-reversion play — A-share retail flows
tend to chase breakouts / panic on dips, which makes the
oversold-bounce signal a long-only viable alpha in practice.

Per CLAUDE.md this strategy is research-grade (P2 W3.2). The
strict A-share patch layer (price-limit, suspension, ST filter,
delisted universe, strict lot enforcement, sell-only stamp tax) is
owned by P1 W4 and is NOT implemented here. ``ChinaStockConfig``
below only enables ``tick_size`` enforcement, identical to the
MA-cross template.

W5.1 ParamSpec notes:
  * Tunable params (IntParam inline fields): ``top_n``,
    ``rsi_window``, ``boll_window``, ``rebalance_weekday``.
    W5 callers tune per fold via
    ``run_backtest(strategy_params={"top_n": 5}, ...)`` or
    top-level kwargs (``run_backtest(..., top_n=5, ...)``).
  * Runtime-only constants (NOT ParamSpec): ``INITIAL_CASH``,
    ``COMMISSION_RATE``, ``STAMP_TAX_RATE``, ``LOT_SIZE`` (passed
    to ``run_backtest`` and ``InstrumentConfig``); ``SYMBOLS`` /
    ``DEFAULT_SYMBOLS`` (universe selection).
  * Derived: ``WINDOW = max(RSI_WINDOW, BOLL_WINDOW)`` and
    ``HISTORY_DEPTH = WINDOW + 5``. Kept module-level ``Final``
    (deriving them from ParamSpec defaults at class-body
    evaluation is brittle — optuna may tune RSI_WINDOW without
    re-deriving WINDOW, which would silently break the warm-up
    window). W5+ can promote to a pydantic model_validator if
    needed.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Final

import akquant
import numpy as np
import pandas as pd
from akquant import ChinaStockConfig, run_backtest
from akquant.backtest.result import BacktestResult
from akquant.config import (
    BacktestConfig,
    InstrumentConfig,
    RiskConfig,
    StrategyConfig,
)
from akquant.params import IntParam
from loguru import logger

# ---------------------------------------------------------------------------
# Constants (shared with the MA-cross template via re-export)
# ---------------------------------------------------------------------------

INITIAL_CASH: Final[float] = 1_000_000.0
COMMISSION_RATE: Final[float] = 0.0003
STAMP_TAX_RATE: Final[float] = 0.001
LOT_SIZE: Final[int] = 100
TARGET_PERCENT: Final[float] = 0.95

# Strategy-specific defaults. These are also the IntParam defaults
# below; module-level ``Final`` is kept so ``ma_cross_duckdb.py`` and
# the existing W3 tests can keep importing them by name (the AKQuant
# ``__init_subclass__`` deletes ParamSpec descriptors from the
# class namespace, so the *class* attribute ``strategy.top_n`` is
# not available — only ``strategy.params.top_n`` is, plus the
# module-level fallback here).
RSI_WINDOW: Final[int] = 14
BOLL_WINDOW: Final[int] = 20
WINDOW: Final[int] = max(RSI_WINDOW, BOLL_WINDOW)
HISTORY_DEPTH: Final[int] = WINDOW + 5
TOP_N: Final[int] = 10  # >= len(SYMBOLS) for the W3.2 e2e universe
REBALANCE_WEEKDAY: Final[int] = 0  # Monday

# Default 4-symbol e2e universe. Real smoke runs would swap in a
# longer list from a universe table (W5+).
DEFAULT_SYMBOLS: Final[tuple[str, ...]] = ("000001", "600000", "000002", "600519")

# ---------------------------------------------------------------------------
# sys.path bootstrap (mirrors ma_cross_duckdb.py)
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from research.factor_lib.mean_reversion import bollinger_z, rsi  # noqa: E402
from research.strategies._multi_symbol_loader import (  # noqa: E402
    load_multi_symbol_bars,
)

__all__ = [
    "BOLL_WINDOW",
    "COMMISSION_RATE",
    "DEFAULT_SYMBOLS",
    "HISTORY_DEPTH",
    "INITIAL_CASH",
    "LOT_SIZE",
    "REBALANCE_WEEKDAY",
    "RSI_WINDOW",
    "STAMP_TAX_RATE",
    "TARGET_PERCENT",
    "TOP_N",
    "WINDOW",
    "TopNMeanReversionStrategy",
    "run_demo",
]


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class TopNMeanReversionStrategy(akquant.Strategy):
    """Top-N oversold mean-reversion, weekly equal-weight rebalance.

    Tunable parameters (W5 walk-forward / optuna):
      * ``top_n`` (int, default 10) — number of oversold symbols
        to long each rebalance.
      * ``rsi_window`` (int, default 14) — Wilder RSI lookback.
      * ``boll_window`` (int, default 20) — Bollinger z lookback.
      * ``rebalance_weekday`` (int, default 0) — Monday=0, ..., Sun=6.

    Runtime-only (not a strategy param, set on ``run_backtest``):
      ``initial_cash``, ``commission_rate``, ``stamp_tax_rate``,
      ``lot_size``, ``t_plus_one``, ``history_depth``,
      ``warmup_period``, ``symbols``.

    On each Monday's ``on_cross_section``:

      1. For every universe symbol, fetch the last ``WINDOW + 1``
         bars of close via ``get_history``.
      2. Compute Wilder RSI (window=``self.params.rsi_window``) and
         Bollinger z (window=``self.params.boll_window``).
      3. Combine: ``score = -(0.5 * (rsi - 50) + 0.5 * z)`` so the
         MOST oversold names (low RSI, negative z) get the HIGHEST
         score.
      4. ``rebalance_to_topn(scores, top_n=self.params.top_n, ...)``
         equal-weights the top N.

    Symbols with insufficient history or NaN factors are skipped
    (so a new listing doesn't tank the cross-section rank for a
    week).
    """

    # Inline ParamSpec fields. AKQuant's ``__init_subclass__`` collects
    # these into a frozen pydantic ``ParamModel`` accessible via
    # ``self.params.<name>`` (the class attribute is ``delattr``'d).
    # W5 callers tune per fold via
    # ``run_backtest(strategy_params={"top_n": 5, ...}, ...)``.
    # ``# type: ignore[assignment]`` because mypy doesn't see the
    # ``__init_subclass__`` magic that converts the ParamSpec to an
    # instance attribute.
    top_n: int = IntParam(TOP_N)  # type: ignore[assignment]
    rsi_window: int = IntParam(RSI_WINDOW)  # type: ignore[assignment]
    boll_window: int = IntParam(BOLL_WINDOW)  # type: ignore[assignment]
    rebalance_weekday: int = IntParam(REBALANCE_WEEKDAY)  # type: ignore[assignment]

    def on_start(self) -> None:
        try:
            restored: bool = bool(self.is_restored)
        except AttributeError:
            restored = False
        _ = restored  # placeholder; no session-state init needed yet
        # HISTORY_DEPTH stays module-level (derived from RSI/BOLL
        # defaults). W5.1 keeps WINDOW / HISTORY_DEPTH Final for
        # backward compatibility; see module docstring.
        self.set_history_depth(HISTORY_DEPTH)

    def on_cross_section(self, trading_date: date, timestamp: int) -> None:
        # Weekly gate: only rebalance on the configured day.
        if trading_date.weekday() != self.params.rebalance_weekday:
            return

        scores: dict[str, float] = {}
        for symbol in self._iter_symbols():
            close_arr = self.get_history(
                count=WINDOW + 1,
                symbol=symbol,
                field="close",
            )
            if close_arr is None or len(close_arr) < WINDOW + 1:
                continue
            close_series = pd.Series(np.asarray(close_arr, dtype=float))
            rsi_series = rsi(close_series, window=self.params.rsi_window)
            z_series = bollinger_z(close_series, window=self.params.boll_window)
            rsi_last = rsi_series.iloc[-1]
            z_last = z_series.iloc[-1]
            if pd.isna(rsi_last) or pd.isna(z_last):
                continue
            # Reverse: oversold (low RSI, negative z) ⇒ high score.
            score = -(0.5 * (float(rsi_last) - 50.0) + 0.5 * float(z_last))
            scores[symbol] = score

        if not scores:
            return
        self.rebalance_to_topn(
            scores=scores,
            top_n=min(self.params.top_n, len(scores)),
            weight_mode="equal",
            long_only=True,
            liquidate_unmentioned=True,
        )

    def _iter_symbols(self) -> Sequence[str]:
        """Return the universe symbols known to the strategy.

        Subclasses can override to drive the universe from a
        different source. Default uses the ``SYMBOLS`` class attr
        if set, otherwise ``DEFAULT_SYMBOLS``.
        """
        symbols: Sequence[str] = getattr(self, "SYMBOLS", DEFAULT_SYMBOLS)
        return symbols


# ---------------------------------------------------------------------------
# Demo runner
# ---------------------------------------------------------------------------


def run_demo(
    duckdb_path: str | Path = "data/duckdb/daily.duckdb",
    symbols: Sequence[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> BacktestResult:
    """Run the TopN mean-reversion strategy end-to-end.

    Uses module-level ``TOP_N`` / ``RSI_WINDOW`` / ``BOLL_WINDOW``
    as the run-time defaults; W5 callers tune per fold via
    ``run_walk_forward(strategy_params={"top_n": 5, ...}, ...)``.

    Args:
        duckdb_path: DuckDB file to read bars from. Defaults to the
            W2.1 production file.
        symbols: Override the universe. Defaults to
            ``DEFAULT_SYMBOLS``.
        start_date: Inclusive ISO start. Defaults to data-layer's
            earliest row for the universe.
        end_date: Inclusive ISO end. Defaults to data-layer's
            latest row for the universe.

    Returns:
        The ``BacktestResult`` so callers (W5 walk-forward, P3
        dashboard) can reuse this entry point.
    """
    universe = tuple(symbols) if symbols is not None else DEFAULT_SYMBOLS
    data_map = load_multi_symbol_bars(duckdb_path, universe, start_date, end_date)
    if not data_map:
        raise RuntimeError(
            f"No bars returned from DuckDB at {duckdb_path} for symbols {universe}; "
            f"check the data layer is populated (see data_layer.ingestion.akshare_fetcher)."
        )
    logger.info(
        "TopN-mean-reversion: {n} symbols, {b} bars each (avg)",
        n=len(data_map),
        b=int(np.mean([len(df) for df in data_map.values()])),
    )

    result = run_backtest(
        data=data_map,
        strategy=TopNMeanReversionStrategy,
        symbols=list(data_map.keys()),
        initial_cash=INITIAL_CASH,
        commission_rate=COMMISSION_RATE,
        stamp_tax_rate=STAMP_TAX_RATE,
        lot_size=LOT_SIZE,
        t_plus_one=True,
        history_depth=HISTORY_DEPTH,
        warmup_period=WINDOW,
        config=BacktestConfig(
            strategy_config=StrategyConfig(
                initial_cash=INITIAL_CASH,
                risk=RiskConfig(max_position_pct=TARGET_PERCENT),
            ),
            instruments_config=[
                InstrumentConfig(
                    symbol=s,
                    asset_type="STOCK",
                    tick_size=0.01,
                    lot_size=LOT_SIZE,
                )
                for s in data_map
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
        "TopN-mean-reversion done: symbols={ns} bars={nb} trades={nt} "
        "total_ret={ret:.2f}% sharpe={sh:.3f} sortino={so:.3f} "
        "mdd={dd:.2%} win_rate={wr:.2f}% profit_factor={pf:.2f}",
        ns=len(data_map),
        nb=int(np.sum([len(df) for df in data_map.values()])),
        nt=len(result.trades_df),
        ret=_row("total_return_pct"),
        sh=_row("sharpe_ratio"),
        so=_row("sortino_ratio"),
        dd=_row("max_drawdown"),
        wr=_row("win_rate"),
        pf=_row("profit_factor"),
    )
    return result


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO", colorize=True)
    run_demo()
