"""Hand-computed golden-output regression tests (W3.1-C8).

These tests lock down the factor library's numerical output against
*external, manually-computed* references. They are the factor-lib
analogue of the ``ma-cross-baseline-000001-20240826`` memory entry
for the MA-cross strategy: any future refactor that drifts these
values must be intentional.

Tolerance is ``atol=1e-9`` everywhere except where the formula
provokes a small numerical instability (RSI Wilder warm-up under
strict monotone-up; we use ``atol=1e-6`` there).

Each test is a frozen regression case — DO NOT change the input
data without also bumping the factor lib version (and the
``[w3-status]`` memory entry) so the change is reviewable.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from research.factor_lib.liquidity import turnover_ratio
from research.factor_lib.mean_reversion import bollinger_z, rsi
from research.factor_lib.momentum import n_day_return
from research.factor_lib.trend import ma_deviation

# ===========================================================================
# Group 1: trend — ma_deviation
# ===========================================================================


def test_golden_ma_deviation_hand_computed() -> None:
    """SMA(5) of [10..16]: indices 4..6 hand-computed below."""
    closes = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]
    close_s = pd.Series(closes, name="close")
    out = ma_deviation(close_s, bar_window=5)

    # Hand-computed values:
    #   index 4: SMA(5) = 12.0; deviation = (14 - 12) / 12 = 0.166666...
    #   index 5: SMA(5) = 13.0; deviation = (15 - 13) / 13 ≈ 0.153846...
    #   index 6: SMA(5) = 14.0; deviation = (16 - 14) / 14 ≈ 0.142857...
    expected = pd.Series(
        [np.nan, np.nan, np.nan, np.nan, 2.0 / 12.0, 2.0 / 13.0, 2.0 / 14.0],
        name="ma_dev_5",
    )
    expected.index = close_s.index
    pd.testing.assert_series_equal(out, expected, atol=1e-9, check_names=True)


# ===========================================================================
# Group 2: momentum — n_day_return
# ===========================================================================


def test_golden_n_day_return_hand_computed() -> None:
    """Window 4 over [10..14]: index 4 = 14/10 - 1 = 0.4."""
    closes = [10.0, 11.0, 12.0, 13.0, 14.0]
    close_s = pd.Series(closes, name="close")
    out = n_day_return(close_s, window=4)

    expected = pd.Series(
        [np.nan, np.nan, np.nan, np.nan, 0.4],
        name="nret_4",
    )
    expected.index = close_s.index
    pd.testing.assert_series_equal(out, expected, atol=1e-9, check_names=True)


# ===========================================================================
# Group 3: mean-reversion — rsi, bollinger_z
# ===========================================================================


def test_golden_rsi_monotonic_up_is_100() -> None:
    """Strictly monotonically rising closes → RSI = 100 (Wilder)."""
    closes = [100.0 + i for i in range(30)]
    close_s = pd.Series(closes, name="close")
    out = rsi(close_s, window=14)

    # After warm-up (window=14 rows forced NaN), the remaining 16
    # rows are all 100.0 (avg_down is identically 0).
    finite = out.iloc[14:]
    expected = pd.Series(100.0, index=finite.index, name="rsi_14")
    pd.testing.assert_series_equal(finite, expected, atol=1e-6, check_names=False)


def test_golden_rsi_monotonic_down_is_0() -> None:
    """Strictly monotonically falling closes → RSI = 0 (Wilder)."""
    closes = [100.0 - i for i in range(30)]
    close_s = pd.Series(closes, name="close")
    out = rsi(close_s, window=14)
    finite = out.iloc[14:]
    expected = pd.Series(0.0, index=finite.index, name="rsi_14")
    pd.testing.assert_series_equal(finite, expected, atol=1e-6, check_names=False)


def test_golden_rsi_flat_is_50() -> None:
    """Constant closes → RSI = 50 (neutral)."""
    closes = [100.0] * 30
    close_s = pd.Series(closes, name="close")
    out = rsi(close_s, window=14)
    finite = out.iloc[14:]
    expected = pd.Series(50.0, index=finite.index, name="rsi_14")
    pd.testing.assert_series_equal(finite, expected, atol=1e-9, check_names=False)


def test_golden_bollinger_z_constant_is_nan() -> None:
    """Constant closes → std=0 → z is NaN, not inf."""
    closes = [5.0] * 30
    close_s = pd.Series(closes, name="close")
    out = bollinger_z(close_s, window=20)
    expected = pd.Series(np.nan, index=close_s.index, name="boll_z_20_2")
    pd.testing.assert_series_equal(out, expected, atol=1e-9, check_names=True)


def test_golden_bollinger_z_at_mean_is_zero() -> None:
    """Close == rolling mean → z = 0 (within float epsilon).

    Series [1..19, X] with X = 10 (chosen so mean == last close).
    """
    closes = [*list(range(1, 20)), 10]
    close_s = pd.Series(closes, name="close")
    out = bollinger_z(close_s, window=20)
    # Only the last row is interesting; warm-up rows are NaN.
    assert out.iloc[-1] == pytest.approx(0.0, abs=1e-9)


# ===========================================================================
# Group 4: liquidity — turnover_ratio
# ===========================================================================


def test_golden_turnover_ratio_basic() -> None:
    """vol=1e6, outstanding=1e10 → turnover = 1e-4 (0.01%) at every bar."""
    vol = pd.Series([1e6, 2e6, 3e6], name="volume")
    out = pd.Series([1e10, 1e10, 1e10], name="outstanding_share")
    ratio = turnover_ratio(vol, out)
    expected = pd.Series([1e-4, 2e-4, 3e-4], name="turnover_ratio")
    expected.index = vol.index
    pd.testing.assert_series_equal(ratio, expected, atol=1e-12, check_names=True)


def test_golden_turnover_ratio_3pct() -> None:
    """3e8 / 1e10 = 0.03 (typical A-share high-liquidity threshold)."""
    vol = pd.Series([3e8], name="volume")
    out = pd.Series([1e10], name="outstanding_share")
    ratio = turnover_ratio(vol, out)
    expected = pd.Series([0.03], name="turnover_ratio")
    expected.index = vol.index
    pd.testing.assert_series_equal(ratio, expected, atol=1e-12, check_names=True)


# ===========================================================================
# Group 5: cross-cutting — factor names + output shape
# ===========================================================================


def test_golden_factor_names_match_documented_format() -> None:
    """Factor name conventions must not drift (they flow into DuckDB
    column names and downstream joins)."""
    closes = pd.Series([10.0] * 30, name="close")
    assert ma_deviation(closes, bar_window=20).name == "ma_dev_20"
    assert n_day_return(closes, window=20).name == "nret_20"
    assert rsi(closes, window=14).name == "rsi_14"
    assert bollinger_z(closes, window=20).name == "boll_z_20_2"
    vol = pd.Series([1e6] * 30, name="volume")
    out = pd.Series([1e10] * 30, name="outstanding_share")
    assert turnover_ratio(vol, out).name == "turnover_ratio"
