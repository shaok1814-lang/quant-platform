"""Tests for ``research/factor_lib/trend.py`` (W3.1-C3)."""

from __future__ import annotations

import pandas as pd
import pytest
from research.factor_lib import MissingColumnError, compute_factor, validate_input_bars
from research.factor_lib.trend import ma_deviation
from tests.conftest import make_bars

# ===========================================================================
# Group 1: warm-up NaN behaviour
# ===========================================================================


@pytest.mark.parametrize(
    "bar_window",
    [
        pytest.param(5, id="window=5"),
        pytest.param(10, id="window=10"),
        pytest.param(20, id="window=20"),
    ],
)
def test_ma_deviation_warmup_nan(bar_window: int) -> None:
    """First ``bar_window - 1`` rows are NaN; remainder is finite."""
    closes = [10.0 + i * 0.1 for i in range(50)]
    close_s = pd.Series(closes)
    out = ma_deviation(close_s, bar_window=bar_window)
    assert out.iloc[: bar_window - 1].isna().all()
    assert out.iloc[bar_window - 1 :].notna().all()


# ===========================================================================
# Group 2: edge cases
# ===========================================================================


def test_ma_deviation_zero_close_returns_nan() -> None:
    """``close == 0`` ⇒ SMA == 0 ⇒ divide-by-zero ⇒ NaN (not inf)."""
    closes = [0.0] * 30
    close_s = pd.Series(closes)
    out = ma_deviation(close_s, bar_window=5)
    # All rows after warm-up must be NaN, not inf.
    assert out.iloc[4:].isna().all()
    # Defensive: no inf anywhere in the frame.
    assert not (out.abs() == float("inf")).any()


def test_ma_deviation_window_lt_one_raises() -> None:
    """``bar_window < 1`` is rejected at the API boundary."""
    with pytest.raises(ValueError, match="bar_window"):
        ma_deviation(pd.Series([10.0]), bar_window=0)


# ===========================================================================
# Group 3: hand-computed reference (drift-anchor for golden test)
# ===========================================================================


def test_ma_deviation_hand_computed_match() -> None:
    """SMA(5) of [10..16]: index 4 = (10+11+12+13+14)/5 = 12.0."""
    closes = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]
    close_s = pd.Series(closes)
    out = ma_deviation(close_s, bar_window=5)
    assert out.iloc[4] == pytest.approx((14.0 - 12.0) / 12.0)
    assert out.iloc[5] == pytest.approx((15.0 - 13.0) / 13.0)
    assert out.iloc[6] == pytest.approx((16.0 - 14.0) / 14.0)


# ===========================================================================
# Group 4: output contract
# ===========================================================================


def test_ma_deviation_output_name() -> None:
    out = ma_deviation(pd.Series([10.0] * 30), bar_window=20)
    assert out.name == "ma_dev_20"


def test_ma_deviation_preserves_index() -> None:
    idx = pd.bdate_range(end=pd.Timestamp("2024-01-12"), periods=30)
    close_s = pd.Series([10.0] * 30, index=idx)
    out = ma_deviation(close_s, bar_window=5)
    assert (out.index == idx).all()


# ===========================================================================
# Group 5: integration with base.validate_input_bars
# ===========================================================================


def test_ma_deviation_via_compute_factor_validates_input() -> None:
    """``compute_factor`` rejects DataFrames missing CORE_COLUMNS_FACTOR."""
    df = make_bars([10.0] * 5).drop(columns=["close"])
    with pytest.raises(MissingColumnError, match="close"):
        validate_input_bars(df)


def test_ma_deviation_via_compute_factor_runs() -> None:
    """``compute_factor(df, ma_deviation, bar_window=5)`` round-trips."""
    df = make_bars([10.0 + i * 0.1 for i in range(30)])
    out = compute_factor(df, ma_deviation, bar_window=5)
    assert out.name == "ma_dev_5"
    assert out.iloc[:4].isna().all()
    assert out.iloc[4:].notna().all()
    assert (out.index == df.index).all()
