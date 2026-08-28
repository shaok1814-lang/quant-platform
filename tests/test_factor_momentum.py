"""Tests for ``research/factor_lib/momentum.py`` (W3.1-C3)."""

from __future__ import annotations

import pandas as pd
import pytest
from research.factor_lib.momentum import n_day_return

# ===========================================================================
# Group 1: warm-up NaN behaviour
# ===========================================================================


@pytest.mark.parametrize(
    "window",
    [
        pytest.param(5, id="window=5"),
        pytest.param(20, id="window=20"),
    ],
)
def test_n_day_return_warmup_nan(window: int) -> None:
    """First ``window`` rows are NaN; remainder is finite."""
    closes = [10.0 + i * 0.01 for i in range(50)]
    close_s = pd.Series(closes)
    out = n_day_return(close_s, window=window)
    assert out.iloc[:window].isna().all()
    assert out.iloc[window:].notna().all()


# ===========================================================================
# Group 2: signed direction
# ===========================================================================


@pytest.mark.parametrize(
    ("closes", "expected"),
    [
        pytest.param([10.0, 11.0, 12.0], 0.2, id="up_returns_positive"),
        pytest.param([10.0, 9.0, 8.0], -0.2, id="down_returns_negative"),
        pytest.param([10.0, 10.0, 10.0], 0.0, id="flat_returns_zero"),
    ],
)
def test_n_day_return_signed_direction(closes: list[float], expected: float) -> None:
    out = n_day_return(pd.Series(closes), window=2)
    assert out.iloc[2] == pytest.approx(expected)


# ===========================================================================
# Group 3: lookahead guard
# ===========================================================================


def test_n_day_return_no_lookahead() -> None:
    """``n_day_return`` must NOT use close[i+1] for the close[i] term.

    With ``window=2``, the return at index 4 must be
    ``close[4]/close[2] - 1`` (past reference), never
    ``close[4]/close[6] - 1`` (future leak).
    """
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
    close_s = pd.Series(closes)
    out = n_day_return(close_s, window=2)
    expected_past = 104.0 / 102.0 - 1.0
    expected_future_leak = 104.0 / 106.0 - 1.0
    assert out.iloc[4] == pytest.approx(expected_past)
    # Sanity: the future-leak value is materially different.
    assert expected_past != pytest.approx(expected_future_leak)
    assert out.iloc[4] != pytest.approx(expected_future_leak)


# ===========================================================================
# Group 4: hand-computed reference
# ===========================================================================


def test_n_day_return_hand_computed() -> None:
    """Window 4 over [10..14]: index 4 = 14/10 - 1 = 0.4."""
    closes = [10.0, 11.0, 12.0, 13.0, 14.0]
    close_s = pd.Series(closes)
    out = n_day_return(close_s, window=4)
    assert out.iloc[4] == pytest.approx(0.4)


# ===========================================================================
# Group 5: edge cases
# ===========================================================================


def test_n_day_return_window_lt_one_raises() -> None:
    with pytest.raises(ValueError, match="window"):
        n_day_return(pd.Series([10.0]), window=0)


def test_n_day_return_zero_close_is_nan() -> None:
    """``close.shift(window) == 0`` ⇒ divide-by-zero ⇒ NaN (not inf)."""
    closes = [0.0, 0.0, 0.0, 0.0, 10.0]
    close_s = pd.Series(closes)
    out = n_day_return(close_s, window=4)
    assert pd.isna(out.iloc[4])
    # Defensive: no inf anywhere.
    assert not (out.abs() == float("inf")).any()


# ===========================================================================
# Group 6: output contract
# ===========================================================================


def test_n_day_return_output_name() -> None:
    out = n_day_return(pd.Series([10.0] * 30), window=20)
    assert out.name == "nret_20"


def test_n_day_return_preserves_index() -> None:
    idx = pd.bdate_range(end=pd.Timestamp("2024-01-12"), periods=30)
    close_s = pd.Series([10.0] * 30, index=idx)
    out = n_day_return(close_s, window=5)
    assert (out.index == idx).all()
