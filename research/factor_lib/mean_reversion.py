"""Mean-reversion factor family.

Covers oscillator-style signals that quantify how far price has
drifted from a local mean relative to its recent dispersion. RSI
flags overbought / oversold via Wilder smoothing; Bollinger z
standardizes the close-to-SMA gap by the rolling standard deviation
so the resulting score is dimensionless and directly comparable
across symbols.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factor_lib._types import FactorSeries


def rsi(close: pd.Series, *, window: int = 14) -> FactorSeries:
    """Wilder's Relative Strength Index in [0, 100].

    Formula (Wilder smoothing):
        delta = close - close.shift(1)
        up    = max(delta, 0)
        down  = max(-delta, 0)
        avg_up   = ewm(up, alpha=1/window).mean()
        avg_down = ewm(down, alpha=1/window).mean()
        rs       = avg_up / avg_down
        rsi      = 100 - 100 / (1 + rs)

    Edge cases:
      * ``avg_down == 0`` (all-up window) → RS = inf → RSI = 100.
      * ``avg_up   == 0`` (all-down window) → RS = 0   → RSI = 0.
      * Both zero (flat window) → RSI = 50 (neutral).
      * First ``window`` rows are forced NaN (Wilder warm-up).

    Args:
        close: Close price ``pd.Series``.
        window: Wilder smoothing window in bars. Must be ``>= 1``.
            Default ``14`` is the canonical Wilder convention.

    Returns:
        ``pd.Series`` aligned to ``close.index`` with values in
        [0, 100] (NaN during warm-up and where both avg_up and
        avg_down are exactly zero for an entire bar). ``name`` is
        ``"rsi_{window}"``.

    Raises:
        ValueError: if ``window < 1``.
    """
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    avg_up = up.ewm(alpha=1.0 / window, adjust=False).mean()
    avg_down = down.ewm(alpha=1.0 / window, adjust=False).mean()

    # Element-wise RS with divide-by-zero guards.
    rs = np.where(avg_down == 0, np.inf, avg_up / np.where(avg_down == 0, 1.0, avg_down))
    # When both are zero (flat window) RS would be NaN — pick 50.
    rs = np.where((avg_up == 0) & (avg_down == 0), 1.0, rs)
    out_arr = 100.0 - 100.0 / (1.0 + rs)
    # When avg_up == 0 (pure downtrend), RS = 0, RSI = 0.
    # When avg_down == 0 (pure uptrend), RS = inf, RSI = 100.
    # When both zero → we set RS = 1 above → 100 - 100/(1+1) = 50. ✓
    out = pd.Series(out_arr, index=close.index)
    out.iloc[:window] = np.nan  # Wilder warm-up
    out.name = f"rsi_{window}"
    return out


def bollinger_z(close: pd.Series, *, window: int = 20, num_std: float = 2.0) -> FactorSeries:
    """Standardized Bollinger band z-score.

    Formula:
        sma = close.rolling(window).mean()
        std = close.rolling(window, ddof=0).std()
        z   = (close - sma) / (num_std * std)

    A value of ``+1`` means price sits exactly ``num_std`` standard
    deviations ABOVE the rolling mean; ``-1`` means exactly
    ``num_std`` BELOW. The score is dimensionless so it can be
    ranked cross-sectionally without per-symbol rescaling.

    Edge cases:
      * First ``window - 1`` rows are NaN (rolling warm-up).
      * ``std == 0`` (constant close over the window) → NaN
        (no dispersion → z is undefined).
      * ``num_std <= 0`` is rejected at the API boundary.

    Args:
        close: Close price ``pd.Series``.
        window: Rolling window in bars. Must be ``>= 1``.
            Default ``20`` matches the slow-MA convention used
            elsewhere in the library.
        num_std: Multiplier on the standard deviation used to
            scale the band distance. Default ``2.0`` is the
            canonical Bollinger band width (covers ~95% of a
            Gaussian).

    Returns:
        ``pd.Series`` aligned to ``close.index``. ``name`` is
        ``"boll_z_{window}_{num_std:g}"``.

    Raises:
        ValueError: if ``window < 1`` or ``num_std <= 0``.
    """
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")
    if num_std <= 0:
        raise ValueError(f"num_std must be > 0, got {num_std}")
    sma = close.rolling(window=window, min_periods=window).mean()
    std = close.rolling(window=window, min_periods=window).std(ddof=0)
    out = (close - sma) / (num_std * std)
    out = out.replace([np.inf, -np.inf], np.nan)
    out.name = f"boll_z_{window}_{num_std:g}"
    return out
