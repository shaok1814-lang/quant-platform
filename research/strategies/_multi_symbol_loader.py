"""Multi-symbol bars loader for AKQuant multi-symbol backtests (W3.2-C1).

Wraps the single-symbol ``DuckStore.query_daily_bars`` API into a
``Dict[str, pd.DataFrame]`` shape that ``akquant.run_backtest``
accepts for cross-section backtests (per AKQuant's
``BacktestDataInput`` union).

Design notes:

  * Per-symbol queries run sequentially. For the W3.2 4-symbol
    smoke universe this is fine; the W5 walk-forward path that
    touches the full A-share universe (~5000 symbols) should
    switch to a single DuckDB ``WHERE symbol IN (...)`` query.
  * Symbols with empty / missing DuckDB rows are silently
    **dropped** from the returned dict. The cross-section strategy
    layer must defensively handle ``len(result) < len(symbols)`` —
    see ``topn_mean_reversion.py`` for the canonical handling.
  * Date bounds are inclusive on both ends, identical to
    ``DuckStore.query_daily_bars`` semantics.

Note the leading underscore in the filename: this is an internal
helper consumed by W3.2 strategy modules, not a public strategy
class. Researchers importing strategies do not need it directly.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from data_layer.storage.duck import DuckStore

__all__ = ["load_multi_symbol_bars"]


def load_multi_symbol_bars(
    duckdb_path: str | Path,
    symbols: list[str] | tuple[str, ...],
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, pd.DataFrame]:
    """Load bars for multiple symbols into ``{symbol: bars_df}``.

    Args:
        duckdb_path: Path to the DuckDB file. Must be writable by
            ``DuckStore`` (parent dir is created on demand).
        symbols: Symbol codes (6-digit A-share strings).
        start_date: Inclusive start (ISO ``YYYY-MM-DD``). ``None``
            means open-ended (use the data layer's earliest row).
        end_date: Inclusive end (ISO ``YYYY-MM-DD``). ``None`` means
            open-ended (use the data layer's latest row).

    Returns:
        ``{symbol: bars_df}`` mapping. Symbols with zero rows in
        DuckDB for the requested date range are silently dropped;
        the caller can ``len(result) < len(symbols)`` to detect
        this.
    """
    out: dict[str, pd.DataFrame] = {}
    if not symbols:
        return out
    with DuckStore(duckdb_path) as store:
        for sym in symbols:
            df = store.query_daily_bars(sym, start_date=start_date, end_date=end_date)
            if not df.empty:
                out[sym] = df
    return out
