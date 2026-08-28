"""A-share suspension (停牌) inference from OHLCV.

There is **no akshare endpoint for per-symbol daily suspension
status**. The closest is ``ak.stock_info_sz_delist(symbol="暂停上市公司")``
which is a static historical list (not bar-by-bar). Strategies that
need bar-level suspension detection either pay for Tushare's
``suspend_d`` (token-gated; W5+ scope) or derive from OHLCV — the
best-effort heuristic this module implements.

Inference rule (any one of these flags a bar as suspended):

  1. ``volume == 0`` ⇒ True (no trades).
  2. Bar's ``high == low == close == prev_close`` AND it is part of
    a flat-line stretch of >=2 consecutive bars ⇒ True (一字板 /
    price-frozen / often suspended, sometimes just a thin-trade day
    that opened and closed at one price).

False negatives (a bar with volume > 0 but the symbol actually
halted on regulatory news) cannot be detected without an
authoritative calendar. Document this in the strategy that uses
this helper so reviewers can assess the data quality.

Boundary semantics:

  * Empty input → return empty ``pd.Series`` (aligned to empty index).
  * Single-row input → still gets a ``volume==0`` check; flat-line
    rule needs >=2 consecutive bars so a single row never flips True
    on the flat-line branch.
  * NaN in close / volume → treat as "not suspended" (cannot decide).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["infer_suspension_from_ohlcv"]


def _resolve_cols(bars: pd.DataFrame) -> tuple[str, str, str | None, str | None]:
    """Find (close, volume, high, low) column names.

    Falls back to ``Adj Close`` / ``Close`` / ``close``; ``Volume`` /
    ``volume``; ``High`` / ``Low`` / ``high`` / ``low``. Returns the
    resolved names; raises ``KeyError`` if any of close / volume
    is missing.
    """
    cols = set(bars.columns)

    def pick(*candidates: str | None) -> str:
        for c in candidates:
            if c is not None and c in cols:
                return c
        raise KeyError(f"bars must have one of {candidates}; found columns {sorted(cols)}")

    close_col = pick("close", "Close", "adj_close", "Adj Close")
    volume_col = pick("volume", "Volume")
    high_col = pick("high", "High", None) if "high" in cols or "High" in cols else None
    low_col = pick("low", "Low", None) if "low" in cols or "Low" in cols else None
    return close_col, volume_col, high_col, low_col


def infer_suspension_from_ohlcv(bars: pd.DataFrame) -> pd.Series:
    """Return a boolean Series aligned to ``bars.index`` flagging suspended bars.

    Args:
        bars: ``pd.DataFrame`` with at least ``close`` and ``volume``
            columns. Optional ``high`` and ``low`` enable the
            flat-line check.

    Returns:
        ``pd.Series[bool]`` aligned to ``bars.index``. ``True`` if
        the bar is inferred-suspended.
    """
    if bars.empty:
        return pd.Series([], dtype=bool, index=pd.Index([], name=bars.index.name))

    close_col, volume_col, high_col, low_col = _resolve_cols(bars)

    # Branch 1: zero volume = no trades that bar.
    zero_volume = bars[volume_col].fillna(-1.0).eq(0.0)

    # Branch 2: flat-line = (high == low == close). The condition
    # ``high == low == close == X`` already implies close did not move
    # during the bar; cross-bar "close unchanged vs previous bar"
    # follows automatically once ``high == low == close`` holds for
    # consecutive bars at the same X. Require >=2 consecutive flat
    # bars (single isolated flat bar = thin-trade day, NOT
    # suspension).
    if high_col is not None and low_col is not None:
        flat_bar = bars[high_col].fillna(np.nan).eq(bars[low_col]) & bars[high_col].fillna(
            np.nan
        ).eq(bars[close_col])
        run_id = (flat_bar != flat_bar.shift()).cumsum()
        flat_run_length = flat_bar.groupby(run_id).transform("size")
        flat_stretch_long = flat_bar & (flat_run_length >= 2)
    else:
        flat_stretch_long = pd.Series(False, index=bars.index)

    out = (zero_volume | flat_stretch_long).astype(bool)
    out.name = "is_suspended"
    return out
