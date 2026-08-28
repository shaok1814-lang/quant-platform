"""Tests for ``research/factor_lib/mean_reversion.py`` (W3.1-C4)."""

from __future__ import annotations

import pandas as pd
import pytest
from research.factor_lib.mean_reversion import bollinger_z, rsi

# ===========================================================================
# Group 1: RSI warm-up NaN behaviour
# ===========================================================================


@pytest.mark.parametrize(
    "window",
    [
        pytest.param(14, id="window=14"),
        pytest.param(20, id="window=20"),
    ],
)
def test_rsi_warmup_nan(window: int) -> None:
    closes = [10.0 + i * 0.01 for i in range(50)]
    close_s = pd.Series(closes)
    out = rsi(close_s, window=window)
    assert out.iloc[:window].isna().all()
    assert out.iloc[window:].notna().all()


# ===========================================================================
# Group 2: RSI boundary cases
# ===========================================================================


def test_rsi_all_up_is_100() -> None:
    """Strictly monotonically rising closes ⇒ RSI → 100."""
    closes = [100.0 + i for i in range(30)]
    close_s = pd.Series(closes)
    out = rsi(close_s, window=14)
    # Last bar is well into the warm-up; should be ~100 (within 1e-6).
    assert out.iloc[-1] == pytest.approx(100.0, abs=1e-6)


def test_rsi_all_down_is_0() -> None:
    """Strictly monotonically falling closes ⇒ RSI → 0."""
    closes = [100.0 - i for i in range(30)]
    close_s = pd.Series(closes)
    out = rsi(close_s, window=14)
    assert out.iloc[-1] == pytest.approx(0.0, abs=1e-6)


def test_rsi_flat_is_50() -> None:
    """Constant closes ⇒ RSI = 50 (neutral)."""
    closes = [100.0] * 30
    close_s = pd.Series(closes)
    out = rsi(close_s, window=14)
    assert out.iloc[-1] == pytest.approx(50.0)


# ===========================================================================
# Group 3: RSI output contract
# ===========================================================================


def test_rsi_window_lt_one_raises() -> None:
    with pytest.raises(ValueError, match="window"):
        rsi(pd.Series([10.0]), window=0)


def test_rsi_output_name() -> None:
    out = rsi(pd.Series([10.0] * 30), window=14)
    assert out.name == "rsi_14"


def test_rsi_preserves_index() -> None:
    idx = pd.bdate_range(end=pd.Timestamp("2024-01-12"), periods=30)
    close_s = pd.Series([10.0 + i for i in range(30)], index=idx)
    out = rsi(close_s, window=14)
    assert (out.index == idx).all()


# ===========================================================================
# Group 4: Bollinger z warm-up NaN behaviour
# ===========================================================================


@pytest.mark.parametrize(
    "window",
    [
        pytest.param(20, id="window=20"),
        pytest.param(5, id="window=5"),
    ],
)
def test_bollinger_z_warmup_nan(window: int) -> None:
    closes = [10.0 + i * 0.1 for i in range(50)]
    close_s = pd.Series(closes)
    out = bollinger_z(close_s, window=window)
    assert out.iloc[: window - 1].isna().all()
    assert out.iloc[window - 1 :].notna().all()


# ===========================================================================
# Group 5: Bollinger z semantics
# ===========================================================================


def test_bollinger_z_at_mean_is_zero() -> None:
    """Close == rolling mean → z = 0 (within float epsilon).

    Series constructed so that the LAST close equals the rolling
    mean: [1..19, X] with X chosen such that (190 + X) / 20 == X,
    i.e. X = 10. Then mean over the window == 10 == last close,
    so the z-score numerator is 0 and z = 0 / (num_std * std) = 0.
    """
    closes_last_equals_mean = [*list(range(1, 20)), 10]
    out = bollinger_z(pd.Series(closes_last_equals_mean), window=20)
    assert out.iloc[-1] == pytest.approx(0.0, abs=1e-9)


def test_bollinger_z_above_mean_is_positive() -> None:
    closes = list(range(1, 21))  # 1..20, mean=10.5, last=20 (above)
    close_s = pd.Series(closes)
    out = bollinger_z(close_s, window=20)
    assert out.iloc[-1] > 0.0


def test_bollinger_z_below_mean_is_negative() -> None:
    closes = list(range(20, 0, -1))  # 20..1, mean=10.5, last=1 (below)
    close_s = pd.Series(closes)
    out = bollinger_z(close_s, window=20)
    assert out.iloc[-1] < 0.0


def test_bollinger_z_zero_std_is_nan() -> None:
    """Constant closes ⇒ rolling std == 0 ⇒ z is NaN, not inf."""
    closes = [5.0] * 30
    close_s = pd.Series(closes)
    out = bollinger_z(close_s, window=20)
    assert pd.isna(out.iloc[-1])
    # Defensive: no inf anywhere.
    assert not (out.abs() == float("inf")).any()


# ===========================================================================
# Group 6: Bollinger z guards
# ===========================================================================


def test_bollinger_z_window_lt_one_raises() -> None:
    with pytest.raises(ValueError, match="window"):
        bollinger_z(pd.Series([10.0]), window=0)


def test_bollinger_z_num_std_lt_zero_raises() -> None:
    with pytest.raises(ValueError, match="num_std"):
        bollinger_z(pd.Series([10.0] * 30), window=20, num_std=0.0)


def test_bollinger_z_num_std_negative_raises() -> None:
    with pytest.raises(ValueError, match="num_std"):
        bollinger_z(pd.Series([10.0] * 30), window=20, num_std=-1.0)


# ===========================================================================
# Group 7: Bollinger z output contract
# ===========================================================================


def test_bollinger_z_output_name() -> None:
    out = bollinger_z(pd.Series([10.0 + i for i in range(30)]), window=20, num_std=2.0)
    assert out.name == "boll_z_20_2"


def test_bollinger_z_custom_num_std_name() -> None:
    out = bollinger_z(pd.Series([10.0 + i for i in range(30)]), window=10, num_std=1.5)
    assert out.name == "boll_z_10_1.5"


def test_bollinger_z_preserves_index() -> None:
    idx = pd.bdate_range(end=pd.Timestamp("2024-01-12"), periods=30)
    close_s = pd.Series([10.0 + i for i in range(30)], index=idx)
    out = bollinger_z(close_s, window=20)
    assert (out.index == idx).all()
