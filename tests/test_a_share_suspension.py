"""Tests for ``backtest/a_share/suspension.py`` (W4-C2)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from backtest.a_share.suspension import infer_suspension_from_ohlcv


def _bars(rows: list[dict[str, float | int]]) -> pd.DataFrame:
    """Convert a row list to a DataFrame indexed by integer position."""
    df = pd.DataFrame(rows)
    return df


# ===========================================================================
# Group 1: volume-zero detection
# ===========================================================================


def test_volume_zero_flags_suspended() -> None:
    """A bar with volume=0 is inferred-suspended (no trades)."""
    bars = _bars(
        [
            {"date": "2024-01-08", "open": 10.0, "high": 10.05, "low": 9.95, "close": 10.0, "volume": 1_000_000},
            {"date": "2024-01-09", "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 0},
        ]
    )
    out = infer_suspension_from_ohlcv(bars)
    assert out.tolist() == [False, True]


def test_volume_zero_with_high_low_above_below() -> None:
    """volume=0 takes precedence even if high != low (impossible in
    practice but the heuristic should be volume-first)."""
    bars = _bars(
        [
            {"date": "2024-01-08", "open": 10.0, "high": 12.0, "low": 9.0, "close": 10.5, "volume": 0},
        ]
    )
    out = infer_suspension_from_ohlcv(bars)
    assert out.tolist() == [True]


# ===========================================================================
# Group 2: flat-line detection (>=2 consecutive)
# ===========================================================================


def test_single_flat_bar_is_not_suspended() -> None:
    """A single flat bar (no surrounding flat context) is NOT flagged —
    it's a thin-trade day, not suspension."""
    bars = _bars(
        [
            {"date": "2024-01-08", "open": 10.0, "high": 10.5, "low": 10.0, "close": 10.3, "volume": 1_000_000},
            {"date": "2024-01-09", "open": 10.3, "high": 10.3, "low": 10.3, "close": 10.3, "volume": 500_000},
            {"date": "2024-01-10", "open": 10.3, "high": 10.6, "low": 10.4, "close": 10.5, "volume": 800_000},
        ]
    )
    out = infer_suspension_from_ohlcv(bars)
    assert out.tolist() == [False, False, False]


def test_two_consecutive_flat_bars_flag_suspended() -> None:
    """Two consecutive flat bars at the same price → both flagged."""
    bars = _bars(
        [
            {"date": "2024-01-08", "open": 10.0, "high": 10.5, "low": 10.0, "close": 10.5, "volume": 1_000_000},
            {"date": "2024-01-09", "open": 10.5, "high": 10.5, "low": 10.5, "close": 10.5, "volume": 800_000},
            {"date": "2024-01-10", "open": 10.5, "high": 10.5, "low": 10.5, "close": 10.5, "volume": 900_000},
            {"date": "2024-01-11", "open": 10.5, "high": 11.0, "low": 10.5, "close": 11.0, "volume": 1_000_000},
        ]
    )
    out = infer_suspension_from_ohlcv(bars)
    # bar 0 = not flat (high != low); bars 1-2 = flat run length 2;
    # bar 3 = not flat. The flat run (bars 1-2) has length 2 >= 2 →
    # both flagged.
    assert out.tolist() == [False, True, True, False]


def test_three_consecutive_flat_bars_all_flagged() -> None:
    """Three flat bars in a row — all three are flagged."""
    bars = _bars(
        [
            {"date": "2024-01-08", "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 500_000},
            {"date": "2024-01-09", "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 500_000},
            {"date": "2024-01-10", "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 500_000},
        ]
    )
    out = infer_suspension_from_ohlcv(bars)
    assert out.tolist() == [True, True, True]


# ===========================================================================
# Group 3: edge cases
# ===========================================================================


def test_empty_input_returns_empty_series() -> None:
    bars = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    out = infer_suspension_from_ohlcv(bars)
    assert len(out) == 0
    assert out.dtype == bool


def test_missing_columns_raises() -> None:
    """Bars missing ``close`` or ``volume`` raise ``KeyError`` (not a
    silent empty result)."""
    bars = pd.DataFrame({"open": [10.0], "high": [10.5], "low": [9.5]})
    with pytest.raises(KeyError):
        infer_suspension_from_ohlcv(bars)


def test_index_alignment() -> None:
    """Output Series index equals input bars.index."""
    idx = pd.date_range("2024-01-08", periods=3, freq="B")
    bars = pd.DataFrame(
        {
            "close": [10.0, 10.0, 10.5],
            "volume": [1_000_000, 0, 1_000_000],
            "high": [10.5, 10.0, 10.6],
            "low": [9.5, 10.0, 10.4],
        },
        index=idx,
    )
    out = infer_suspension_from_ohlcv(bars)
    assert (out.index == idx).all()
    assert out.name == "is_suspended"


def test_no_high_low_falls_back_to_volume_only() -> None:
    """If ``high`` / ``low`` are absent, only the volume check fires."""
    bars = pd.DataFrame(
        {
            "close": [10.0, 10.0, 10.0],
            "volume": [1_000_000, 0, 1_000_000],
        }
    )
    out = infer_suspension_from_ohlcv(bars)
    assert out.tolist() == [False, True, False]


def test_nan_volume_treated_as_not_zero() -> None:
    """NaN volume is NOT zero, so the bar is not flagged via the
    volume branch (NaN means we cannot decide, default to False)."""
    bars = pd.DataFrame(
        {
            "close": [10.0, 10.0],
            "volume": [1_000_000, np.nan],
            "high": [10.5, 10.0],
            "low": [9.5, 10.0],
        }
    )
    out = infer_suspension_from_ohlcv(bars)
    assert out.tolist() == [False, False]


# ===========================================================================
# Group 4: composite scenarios
# ===========================================================================


def test_suspension_via_volume_zero_during_trending_market() -> None:
    """Mid-trend volume-zero (suspension event) is correctly flagged."""
    bars = pd.DataFrame(
        {
            "close": [10.0, 10.5, 10.5, 11.0, 11.0],
            "volume": [1_000_000, 1_500_000, 0, 1_200_000, 1_800_000],
            "high": [10.5, 11.0, 10.5, 11.5, 11.5],
            "low": [9.5, 10.0, 10.5, 10.5, 10.5],
        }
    )
    out = infer_suspension_from_ohlcv(bars)
    # Only the volume=0 bar in the middle is suspended.
    assert out.tolist() == [False, False, True, False, False]
