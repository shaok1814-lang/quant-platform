"""Trend factor family.

Covers price-vs-moving-average signals: how far the current price has
drifted from its recent trend baseline. A positive value means price
is above the moving average (uptrend); a negative value means below
(downtrend). The normalized form (``(close - SMA) / SMA``) makes the
factor scale-invariant so it is comparable across symbols with very
different price levels (e.g. 5-yuan vs 500-yuan A-share names).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factor_lib._types import FactorSeries


def ma_deviation(close: pd.Series, *, bar_window: int = 20) -> FactorSeries:
    """Normalized deviation from a simple moving average.

    Formula: ``(close - SMA(close, bar_window)) / SMA(close, bar_window)``

    Args:
        close: Close price ``pd.Series`` (oldest first). May contain
            NaN — they propagate through the SMA and stay NaN in the
            output.
        bar_window: SMA lookback window in bars. Must be ``>= 1``.
            Default ``20`` matches the slow-MA convention used by the
            existing MA-cross strategy.

    Returns:
        ``pd.Series`` aligned to ``close.index``:

          * First ``bar_window - 1`` rows are NaN (SMA warm-up).
          * ``±inf`` rows (caused by SMA == 0) are coerced to NaN
            so downstream consumers can mask them uniformly.
          * ``name`` is ``"ma_dev_{bar_window}"`` so pipelines can
            ``.melt`` directly without renaming.

    Raises:
        ValueError: if ``bar_window < 1``.
    """
    if bar_window < 1:
        raise ValueError(f"bar_window must be >= 1, got {bar_window}")
    sma = close.rolling(window=bar_window, min_periods=bar_window).mean()
    out = (close - sma) / sma
    out = out.replace([np.inf, -np.inf], np.nan)
    out.name = f"ma_dev_{bar_window}"
    return out
