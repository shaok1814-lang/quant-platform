"""Tests for ``research/factor_lib/post.py`` (W3.1-C6)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from research.factor_lib.post import (
    Neutralizer,
    PassThroughNeutralizer,
    standardize,
    winsorize,
)

# ===========================================================================
# Group 1: winsorize — 3sigma
# ===========================================================================


def test_winsorize_3sigma_clamps_outliers() -> None:
    """A value above the clip bound is clipped down.

    With ``sigma=1`` (not the default 3) the bound sits well below
    the extreme value 100 so the test is deterministic regardless of
    how much the mean / std get inflated by the outlier itself.
    """
    s = pd.Series([10.0, 10.0, 10.0, 10.0, 10.0, 100.0])
    out = winsorize(s, method="3sigma", sigma=1.0)
    assert out.iloc[-1] < 100.0  # clipped down
    # Inliers unchanged.
    assert out.iloc[0] == 10.0
    assert out.iloc[4] == 10.0


def test_winsorize_3sigma_preserves_inliers() -> None:
    """Non-outlier values pass through unchanged."""
    s = pd.Series([9.0, 10.0, 11.0])
    out = winsorize(s, method="3sigma", sigma=3.0)
    assert (out == s).all()


# ===========================================================================
# Group 2: winsorize — mad
# ===========================================================================


def test_winsorize_mad_clamps_outliers() -> None:
    """MAD-based clipping survives extreme outliers in the bound
    computation itself."""
    # Series with one extreme outlier at index 0.
    # median = 10, MAD = 0.5 (all central values), then 1.4826*0.5*3.5
    # = ~2.595 → upper bound ~12.6.
    s = pd.Series([10.0, 10.5, 9.5, 10.5, 9.5, 100.0])
    out = winsorize(s, method="mad", mad_k=3.5)
    assert out.iloc[-1] < 100.0  # clipped
    # Inliers unchanged.
    assert out.iloc[0] == 10.0
    assert out.iloc[1] == 10.5


def test_winsorize_mad_falls_back_to_3sigma_when_mad_zero() -> None:
    """Constant series ⇒ MAD = 0 ⇒ fall back to ``"3sigma"`` branch."""
    s = pd.Series([5.0] * 10)
    out = winsorize(s, method="mad", mad_k=3.5)
    # std of constant series = 0; both branches return unchanged.
    assert (out == 5.0).all()


# ===========================================================================
# Group 3: winsorize — quantile
# ===========================================================================


@pytest.mark.parametrize(
    ("lower_q", "upper_q"),
    [
        pytest.param(0.01, 0.99, id="1pct-99pct"),
        pytest.param(0.05, 0.95, id="5pct-95pct"),
    ],
)
def test_winsorize_quantile_clamps(lower_q: float, upper_q: float) -> None:
    """Quantile clip pulls extremes into the [q_low, q_high] range."""
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 100.0])
    out = winsorize(s, method="quantile", lower_q=lower_q, upper_q=upper_q)
    # The 100.0 extreme must be clipped to <= upper_q quantile.
    upper = s.quantile(upper_q)
    assert out.iloc[-1] <= upper


# ===========================================================================
# Group 4: winsorize — boundary cases
# ===========================================================================


def test_winsorize_empty_returns_empty() -> None:
    s = pd.Series([], dtype=float)
    out = winsorize(s, method="3sigma")
    assert len(out) == 0


def test_winsorize_all_equal_returns_unchanged() -> None:
    """Constant series ⇒ std / MAD == 0 ⇒ return unchanged."""
    s = pd.Series([5.0] * 10)
    out = winsorize(s, method="3sigma")
    assert (out == s).all()


def test_winsorize_all_nan_returns_unchanged() -> None:
    s = pd.Series([np.nan] * 5)
    out = winsorize(s, method="3sigma")
    assert out.isna().all()
    assert len(out) == len(s)


def test_winsorize_preserves_nan_positions() -> None:
    """NaN positions must NOT shift to the clipped bound."""
    s = pd.Series([1.0, np.nan, 2.0, 100.0, np.nan])
    out = winsorize(s, method="3sigma")
    assert pd.isna(out.iloc[1])
    assert pd.isna(out.iloc[4])
    # Non-NaN inliers still finite.
    assert out.iloc[0] == 1.0
    assert out.iloc[2] == 2.0


def test_winsorize_unknown_method_raises() -> None:
    s = pd.Series([1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match="Unknown winsorize method"):
        winsorize(s, method="bogus")  # type: ignore[arg-type]


# ===========================================================================
# Group 5: standardize
# ===========================================================================


def test_standardize_basic_mean_zero_std_one() -> None:
    """Standardize ⇒ mean ~ 0, std ~ 1 (ddof=0 by default)."""
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = standardize(s, ddof=0)
    assert out.mean() == pytest.approx(0.0, abs=1e-9)
    assert out.std(ddof=0) == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize(
    "ddof",
    [pytest.param(0, id="ddof=0"), pytest.param(1, id="ddof=1")],
)
def test_standardize_ddof(ddof: int) -> None:
    """Both ``ddof=0`` (population) and ``ddof=1`` (sample) reach the
    requested std within float epsilon (population / sample differ
    only by a factor of sqrt(N/(N-1)) in the std magnitude; the
    standardized series' own std matches ``ddof`` by definition)."""
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = standardize(s, ddof=ddof)
    assert out.std(ddof=ddof) == pytest.approx(1.0, abs=1e-9)


def test_standardize_preserves_nan() -> None:
    s = pd.Series([1.0, 2.0, np.nan, 4.0, 5.0])
    out = standardize(s)
    assert pd.isna(out.iloc[2])
    # Mean / std computed on the 4 non-NaN values:
    # mean = 3.0, std (ddof=0) = sqrt(2.5). The first value's
    # z-score is (1 - 3) / sqrt(2.5); the second is (5 - 3) / sqrt(2.5).
    expected_first = (1.0 - 3.0) / np.sqrt(2.5)
    expected_last = (4.0 - 3.0) / np.sqrt(2.5)
    assert out.iloc[0] == pytest.approx(expected_first, abs=1e-9)
    assert out.iloc[3] == pytest.approx(expected_last, abs=1e-9)


def test_standardize_single_value_returns_unchanged() -> None:
    """Single non-NaN value ⇒ std = NaN ⇒ return unchanged."""
    s = pd.Series([5.0])
    out = standardize(s)
    assert (out == s).all()


def test_standardize_all_equal_returns_unchanged() -> None:
    """Constant series ⇒ std = 0 ⇒ return unchanged (no divide-by-zero)."""
    s = pd.Series([5.0] * 5)
    out = standardize(s)
    assert (out == s).all()


def test_standardize_empty_returns_empty() -> None:
    s = pd.Series([], dtype=float)
    out = standardize(s)
    assert len(out) == 0


def test_standardize_all_nan_returns_unchanged() -> None:
    s = pd.Series([np.nan] * 5)
    out = standardize(s)
    assert out.isna().all()


# ===========================================================================
# Group 6: Neutralizer Protocol + PassThroughNeutralizer
# ===========================================================================


def test_pass_through_neutralizer_returns_copy() -> None:
    """PassThroughNeutralizer must return a copy (not the input
    reference) so caller-side mutations don't leak."""
    df = pd.DataFrame({"factor": [1.0, 2.0, 3.0], "industry": ["A", "B", "A"]})
    pt = PassThroughNeutralizer()
    out = pt(df, "factor", "industry")
    assert out is not df
    assert out.equals(df)
    # Caller-side mutation does NOT leak back to the input.
    out.iloc[0, 0] = 999.0
    assert df.iloc[0, 0] == 1.0


def test_neutralizer_protocol_accepts_subclass() -> None:
    """A class that implements the Neutralizer Protocol's __call__
    signature is structurally accepted by the Protocol."""

    class GroupMeanNeutralizer:
        def __call__(
            self,
            df_wide: pd.DataFrame,
            factor_col: str,
            group_col: str,
        ) -> pd.DataFrame:
            out = df_wide.copy()
            out[factor_col] = (
                df_wide[factor_col] - df_wide.groupby(group_col)[factor_col].transform("mean")
            )
            return out

    # Static duck-type check: assignable to the Protocol-typed var.
    neutralizer: Neutralizer = GroupMeanNeutralizer()
    df = pd.DataFrame({"factor": [1.0, 2.0, 10.0, 20.0], "industry": ["A", "A", "B", "B"]})
    out = neutralizer(df, "factor", "industry")
    # Group A: factor group-mean = 1.5 → residuals -0.5, 0.5.
    # Group B: factor group-mean = 15.0 → residuals -5.0, 5.0.
    assert out["factor"].iloc[0] == pytest.approx(-0.5)
    assert out["factor"].iloc[1] == pytest.approx(0.5)
    assert out["factor"].iloc[2] == pytest.approx(-5.0)
    assert out["factor"].iloc[3] == pytest.approx(5.0)
    # And the input is untouched.
    assert df["factor"].iloc[0] == 1.0


def test_pass_through_neutralizer_preserves_dtypes() -> None:
    df = pd.DataFrame(
        {
            "factor": [1.0, 2.0, 3.0],
            "industry": pd.Categorical(["A", "B", "A"]),
        }
    )
    out = PassThroughNeutralizer()(df, "factor", "industry")
    # Categorical dtype round-trips.
    assert isinstance(out["industry"].dtype, pd.CategoricalDtype)
