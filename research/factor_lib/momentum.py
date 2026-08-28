"""Momentum factor family.

Pure time-series momentum: how much price has moved over the last
``window`` bars. Positive = uptrend, negative = downtrend, zero =
flat. Cross-sectional use requires ranking within each date (handled
upstream by ``FactorPipeline`` / strategy layer, not here).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factor_lib._types import FactorSeries


def n_day_return(close: pd.Series, *, window: int = 20) -> FactorSeries:
    """N-day simple return.

    Formula: ``close / close.shift(window) - 1``

    Args:
        close: Close price ``pd.Series`` (oldest first). May contain
            NaN — they propagate through the shift and stay NaN.
        window: Lookback in bars. Must be ``>= 1``. Default ``20``
            (roughly one trading month) is the canonical W3 momentum
            lookback.

    Returns:
        ``pd.Series`` aligned to ``close.index``:

          * First ``window`` rows are NaN (shift warm-up).
          * ``±inf`` rows (caused by past close == 0) are coerced to
            NaN.
          * ``name`` is ``"nret_{window}"``.

    Raises:
        ValueError: if ``window < 1``.

    Note on lookahead bias:
        This function does NOT auto-apply ``close.shift(1)`` — the
        shift is the caller's responsibility. ``n_day_return`` itself
        is a pure price-to-past-price ratio. If a decision-time
        signal must avoid using the latest bar (typical for
        end-of-bar decisions: the latest close has not yet been
        observed), call ``n_day_return(close.shift(1), window=N)``
        or ``compute_factor(df, lambda c: n_day_return(c, window=N),
        bar_window=...)`` upstream.

        The factor library does NOT silently insert ``.shift(1)``
        because that would hide the bias from factor consumers and
        make the golden-output regression tests ambiguous.
    """
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")
    out = close / close.shift(window) - 1.0
    out = out.replace([np.inf, -np.inf], np.nan)
    out.name = f"nret_{window}"
    return out
