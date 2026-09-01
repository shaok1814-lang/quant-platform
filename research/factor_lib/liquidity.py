"""Liquidity factor family.

The W3 first slice ships a single liquidity proxy: turnover ratio,
i.e. daily traded volume divided by total outstanding shares. This
approximates the percentage of float traded in a single bar; values
above ~0.03 (~3% daily) are typically considered high-liquidity for
A-share names.

Why ``volume / outstanding_share`` and not the akshare-reported
``turnover`` column directly?
    akshare 1.18 dropped the ``turnover`` column intermittently and
    baostock never returned it; the W2 schema demoted turnover to
    application-level derivation (see ``data_layer/validation``
    history). This factor makes that derivation explicit and
    testable.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factor_lib._types import FactorSeries


def turnover_ratio(
    volume: pd.Series,
    outstanding_share: pd.Series | None,
) -> FactorSeries:
    """Volume-to-outstanding-share turnover ratio.

    Formula: ``volume / outstanding_share`` (element-wise).

    Args:
        volume: Per-bar traded volume ``pd.Series`` (shares).
        outstanding_share: Per-bar total outstanding share count
            ``pd.Series``, OR ``None`` if the data layer did not
            supply this column (it is OPTIONAL in
            ``CORE_COLUMNS_FACTOR``).

    Returns:
        ``pd.Series`` aligned to ``volume.index``:

            * ``outstanding_share is None`` → all-NaN Series (the
              factor library does NOT raise; absence of denominator
              data is a known data-layer state, not a bug).
            * ``outstanding_share[i] == 0`` or NaN → result[i] = NaN
              (denominator undefined; division-by-zero silently
              becomes NaN, not inf).
            * ``volume[i] == 0`` → result[i] = 0 (valid: zero trades
              that bar; zero turnover is meaningful information,
              not a missing-data flag).
            * ``name`` is always ``"turnover_ratio"`` regardless of
              which branch fired (helps pipelines treat the column
              uniformly).

    Note:
        This function intentionally does NOT call
        ``compute_factor`` / ``validate_input_bars`` because its
        shape (``volume``, ``outstanding_share``) does not match the
        single-``close``-Series contract of the trend / momentum /
        mean-reversion families. Callers feed the columns directly.
    """
    if outstanding_share is None:
        out = pd.Series(np.nan, index=volume.index)
    else:
        # ``replace`` covers the 0-denominator case; ``where`` then
        # masks both 0 and NaN outstanding values so downstream
        # consumers see NaN (not inf) for invalid denominators.
        raw = volume / outstanding_share
        raw = raw.replace([np.inf, -np.inf], np.nan)
        out = raw.where(outstanding_share > 0)
    out.name = "turnover_ratio"
    return out
