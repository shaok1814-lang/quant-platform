"""Dashboard data loaders (W6.2.1).

Pure-function loaders / wrappers for the Streamlit dashboard
(:mod:`ops.dashboard`). Kept SEPARATE from the Streamlit entry so
they can be unit-tested without spinning up the Streamlit server
(``@st.cache_data`` would force the test into the Streamlit
runtime).

Modules exposed:

  * :func:`load_universe_status` — per-symbol row count, last_dt,
    fetcher distribution. Drives the "Universe Status" page.
  * :func:`load_symbol_bars` — single-symbol OHLCV frame.
  * :func:`load_multi_symbol_universe` — multi-symbol frame dict
    (matches ``akquant.run_backtest``'s ``Dict[str, pd.DataFrame]``
    contract).
  * :func:`compute_strategy_equity` — wrap
    :func:`akquant.run_backtest` and return the per-symbol equity
    curve + summary stats.

The functions do NOT raise on an empty DuckDB — they return
empty frames / empty dicts so the Streamlit UI shows a friendly
"No data yet" message instead of an error stack trace.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pandas as pd
from data_layer.storage.duck import DuckStore
from loguru import logger

if TYPE_CHECKING:
    from akquant.backtest.result import BacktestResult

__all__ = [
    "DEFAULT_DUCKDB_PATH",
    "compute_strategy_equity",
    "load_multi_symbol_universe",
    "load_symbol_bars",
    "load_universe_status",
]


_PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_DUCKDB_PATH: Final[Path] = _PROJECT_ROOT / "data" / "duckdb" / "daily.duckdb"


# ---------------------------------------------------------------------------
# Universe status
# ---------------------------------------------------------------------------


def load_universe_status(duckdb_path: str | Path | None = None) -> pd.DataFrame:
    """Aggregate daily_bars per symbol for the Universe Status page.

    Returns a DataFrame with columns:

      * ``symbol`` — 6-digit A-share symbol
      * ``n_rows`` — number of bars in DuckDB for this symbol
      * ``first_dt`` — earliest bar date
      * ``last_dt`` — latest bar date
      * ``n_trading_days`` — business-day count between first_dt
        and last_dt (gap detector)
      * ``fetchers`` — comma-separated unique fetcher labels for
        this symbol's bars (typically ``"akshare"`` or
        ``"baostock"``).

    Returns an empty DataFrame with the same columns if the
    table is empty / missing.
    """
    db_path = Path(duckdb_path) if duckdb_path is not None else DEFAULT_DUCKDB_PATH
    cols = ["symbol", "n_rows", "first_dt", "last_dt", "n_trading_days", "fetchers"]
    if not db_path.exists():
        return pd.DataFrame(columns=cols)

    with DuckStore(db_path) as store:
        df = store.conn.execute("""
            SELECT
                symbol,
                COUNT(*)                                                                AS n_rows,
                MIN(date)                                                               AS first_dt,
                MAX(date)                                                               AS last_dt,
                CAST(COUNT(*) AS BIGINT)                                                AS n_trading_days_raw,
                LIST(DISTINCT fetcher)                                                  AS fetchers_list
            FROM daily_bars
            GROUP BY symbol
            ORDER BY symbol
        """).df()

    if df.empty:
        return pd.DataFrame(columns=cols)

    df["fetchers"] = df["fetchers_list"].apply(
        lambda lst: ", ".join(sorted(x for x in lst if x)) if lst is not None else ""
    )
    df["n_trading_days"] = df.apply(
        lambda row: int(pd.bdate_range(row["first_dt"], row["last_dt"]).size),
        axis=1,
    )
    return df[cols]


# ---------------------------------------------------------------------------
# Single-symbol bars
# ---------------------------------------------------------------------------


def load_symbol_bars(
    symbol: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    duckdb_path: str | Path | None = None,
) -> pd.DataFrame:
    """OHLCV bars for one symbol. Both date bounds inclusive, either
    optional.

    Returns an empty DataFrame on missing symbol (NOT an exception)
    so the chart code can show "no data yet" cleanly.
    """
    db_path = Path(duckdb_path) if duckdb_path is not None else DEFAULT_DUCKDB_PATH
    if not db_path.exists():
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
    with DuckStore(db_path) as store:
        return store.query_daily_bars(symbol, start_date, end_date)


# ---------------------------------------------------------------------------
# Multi-symbol universe
# ---------------------------------------------------------------------------


def load_multi_symbol_universe(
    symbols: list[str],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    duckdb_path: str | Path | None = None,
) -> dict[str, pd.DataFrame]:
    """``Dict[symbol, OHLCV_frame]`` for the given symbols. Symbols
    with zero rows are omitted (NOT raised) so AKQuant doesn't
    fail on an ETF-without-data case.

    Mirrors the contract of
    :func:`research.strategies._multi_symbol_loader.load_multi_symbol_bars`
    so the dashboard can reuse the same backtest code path.
    """
    return {
        sym: load_symbol_bars(
            sym, start_date=start_date, end_date=end_date, duckdb_path=duckdb_path
        )
        for sym in symbols
        if not load_symbol_bars(
            sym, start_date=start_date, end_date=end_date, duckdb_path=duckdb_path
        ).empty
    }


# ---------------------------------------------------------------------------
# Strategy backtest wrapper
# ---------------------------------------------------------------------------


def compute_strategy_equity(
    strategy_cls: type,
    *,
    data: pd.DataFrame | dict[str, pd.DataFrame],
    strategy_params: dict[str, Any] | None = None,
    initial_cash: float = 1_000_000.0,
) -> tuple[pd.Series, BacktestResult]:
    """Run ``akquant.run_backtest`` and return both:

      * ``equity_curve`` — :class:`pandas.Series` with the backtest's
        per-bar / per-day NAV (what the Streamlit chart plots).
      * ``result`` — the raw :class:`BacktestResult` so the UI can
        show metrics (Sharpe, Sortino, MDD, etc.) without re-running.

    Args:
        strategy_cls: An AKQuant ``Strategy`` subclass (e.g.
            :class:`research.strategies.ma_cross.MACrossStrategy`).
        data: Either a single ``pd.DataFrame`` (one symbol) or
            ``Dict[symbol, pd.DataFrame]`` (multi-symbol universe).
        strategy_params: Optional kwargs forwarded to the strategy.
            For W5.1-promoted strategies these land via
            ``self.params.<name>`` (see [[w5-1-status]]).
        initial_cash: Default 1M CNY (matches all baseline backtests
            in the W1 / W5 captures).

    Returns:
        ``(equity_curve, BacktestResult)`` tuple. If the backtest
        returns no equity points (zero-trade run), ``equity_curve``
        is an empty :class:`pandas.Series`.
    """
    import akquant

    params = dict(strategy_params or {})
    result = akquant.run_backtest(
        data=data,
        strategy=strategy_cls,
        initial_cash=initial_cash,
        **params,
    )
    equity: pd.Series = result.equity_curve
    if equity is None or len(equity) == 0:
        logger.warning(
            "strategy {cls} returned empty equity_curve on data with {n} symbols",
            cls=strategy_cls.__name__,
            n=len(data) if isinstance(data, dict) else 1,
        )
    return equity, result
