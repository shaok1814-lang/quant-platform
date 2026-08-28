"""A-share ex-dividend / ex-rights (除权除息) detection.

The data layer's qfq-adjusted bars (W2.1 contract) are the canonical
source — ``adj_factor`` is the qfq adjustment factor carried per bar.
This module is the backtest-layer mirror: given a bars DataFrame with
an ``adj_factor`` column, it flags the rows where the factor jumps
(those are the ex-div / split dates).

Note: AKQuant itself does NOT enforce a specific ex-div handling
strategy — it relies on the data layer to provide adjusted prices.
W4 ships a detector so a backtest can sanity-check the data layer
output (catches "qfq factor was not applied" data bugs early).
"""

from __future__ import annotations

from typing import Final

import pandas as pd

__all__ = ["detect_ex_dividend_days"]

# A change in adj_factor > this threshold is treated as a real
# ex-div / split event. Smaller values are common float-noise from
# akshare / baostock and should be ignored.
_EX_DIV_PCT_THRESHOLD: Final[float] = 1e-6


def detect_ex_dividend_days(
    bars: pd.DataFrame,
    *,
    adjustment_factor_col: str = "adj_factor",
) -> list[pd.Timestamp]:
    """Return the timestamps of bars where ``adj_factor`` jumps.

    Args:
        bars: ``pd.DataFrame`` with at least ``date`` (datetime-like)
            and ``adj_factor`` columns.
        adjustment_factor_col: Name of the qfq adjustment-factor
            column. Default ``"adj_factor"``.

    Returns:
        ``list[pd.Timestamp]`` of dates where the pct change in the
        adjustment factor exceeds :data:`_EX_DIV_PCT_THRESHOLD`.
        First bar is never flagged (no prior reference).

    Raises:
        KeyError: if ``bars`` does not carry the requested
            ``adjustment_factor_col``.
    """
    if adjustment_factor_col not in bars.columns:
        raise KeyError(
            f"bars missing required column {adjustment_factor_col!r}; "
            f"available columns: {sorted(bars.columns)}"
        )
    factor = bars[adjustment_factor_col]
    pct_change = factor.pct_change().abs()
    flagged = pct_change[pct_change > _EX_DIV_PCT_THRESHOLD]
    if flagged.empty:
        return []
    # If bars is indexed by date, ``flagged.index`` is already the
    # corresponding DatetimeIndex slice; otherwise fall back to the
    # ``date`` column or the integer index.
    if isinstance(bars.index, pd.DatetimeIndex):
        dates = list(flagged.index)
    elif "date" in bars.columns:
        dates = list(bars.loc[flagged.index, "date"])
    else:
        dates = list(flagged.index)
    return [pd.Timestamp(d) for d in dates]
