"""Tests for ``backtest/a_share/ex_dividend.py`` (W4-C4)."""

from __future__ import annotations

import pandas as pd
import pytest
from backtest.a_share.ex_dividend import detect_ex_dividend_days

# ===========================================================================
# Group 1: detection
# ===========================================================================


def test_detect_ex_dividend_days_flags_adj_factor_jump() -> None:
    """Adj factor drops from 1.0 to 0.95 at bar 3 (5% dividend) → bar 3 flagged."""
    bars = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-08", periods=4, freq="B"),
            "adj_factor": [1.0, 1.0, 0.95, 0.95],
        }
    )
    out = detect_ex_dividend_days(bars)
    assert len(out) == 1
    assert out[0] == pd.Timestamp("2024-01-10")


def test_detect_ex_dividend_days_constant_factor_no_flags() -> None:
    """Constant adj_factor (no ex-div / split) → empty list."""
    bars = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-08", periods=5, freq="B"),
            "adj_factor": [1.0, 1.0, 1.0, 1.0, 1.0],
        }
    )
    out = detect_ex_dividend_days(bars)
    assert out == []


def test_detect_ex_dividend_days_multiple_jumps() -> None:
    """Two distinct ex-div events → two flagged dates."""
    bars = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=5, freq="B"),
            # 5% dividend at bar 2; 10% split at bar 4.
            # Bars 1, 3 hold constant → not flagged (0% change).
            "adj_factor": [1.0, 1.0, 0.95, 0.95, 0.90],
        }
    )
    out = detect_ex_dividend_days(bars)
    assert len(out) == 2
    assert out[0] == pd.Timestamp("2024-01-03")
    assert out[1] == pd.Timestamp("2024-01-05")


def test_detect_ex_dividend_days_ignores_float_noise() -> None:
    """Pct change below ``_EX_DIV_PCT_THRESHOLD`` (1e-6) is ignored."""
    bars = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-08", periods=3, freq="B"),
            "adj_factor": [1.0, 1.0 + 1e-9, 1.0 + 2e-9],  # tiny noise
        }
    )
    out = detect_ex_dividend_days(bars)
    assert out == []


# ===========================================================================
# Group 2: index / column handling
# ===========================================================================


def test_detect_ex_dividend_days_works_with_date_column() -> None:
    """Non-DatetimeIndex with a ``date`` column works (returns Timestamps)."""
    bars = pd.DataFrame(
        {
            "date": ["2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11"],
            "adj_factor": [1.0, 1.0, 0.95, 0.95],
        }
    )
    out = detect_ex_dividend_days(bars)
    assert len(out) == 1
    assert out[0] == pd.Timestamp("2024-01-10")


def test_detect_ex_dividend_days_works_with_datetimeindex() -> None:
    """DatetimeIndex case (data layer's typical output shape)."""
    idx = pd.date_range("2024-01-08", periods=4, freq="B")
    bars = pd.DataFrame({"adj_factor": [1.0, 1.0, 0.95, 0.95]}, index=idx)
    out = detect_ex_dividend_days(bars)
    assert len(out) == 1
    assert out[0] == pd.Timestamp("2024-01-10")


# ===========================================================================
# Group 3: validation
# ===========================================================================


def test_detect_ex_dividend_days_raises_on_missing_column() -> None:
    bars = pd.DataFrame({"date": ["2024-01-08"], "wrong_col": [1.0]})
    with pytest.raises(KeyError, match="adj_factor"):
        detect_ex_dividend_days(bars)


def test_detect_ex_dividend_days_custom_column_name() -> None:
    """``adjustment_factor_col`` kwarg overrides the default column name."""
    bars = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-08", periods=3, freq="B"),
            "my_qfq_factor": [1.0, 1.0, 0.95],
        }
    )
    out = detect_ex_dividend_days(bars, adjustment_factor_col="my_qfq_factor")
    assert len(out) == 1
