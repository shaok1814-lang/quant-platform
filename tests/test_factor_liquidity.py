"""Tests for ``research/factor_lib/liquidity.py`` (W3.1-C5)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from research.factor_lib.liquidity import turnover_ratio
from tests.conftest import make_bars

# ===========================================================================
# Group 1: basic happy-path
# ===========================================================================


def test_turnover_ratio_basic() -> None:
    """1e6 shares / 1e10 outstanding = 1e-4 (0.01%) per bar."""
    vol = pd.Series([1e6, 2e6, 3e6])
    out = pd.Series([1e10, 1e10, 1e10])
    ratio = turnover_ratio(vol, out)
    assert ratio.iloc[0] == pytest.approx(1e-4)
    assert ratio.iloc[1] == pytest.approx(2e-4)
    assert ratio.iloc[2] == pytest.approx(3e-4)


def test_turnover_ratio_typical_a_share_magnitude() -> None:
    """3% daily turnover is a typical "high-liquidity" threshold for
    A-share names. Construct a synthetic frame with volume=3e8 on a
    1e10 share float and confirm the ratio sits near 0.03.
    """
    vol = pd.Series([3e8])
    out = pd.Series([1e10])
    ratio = turnover_ratio(vol, out)
    assert ratio.iloc[0] == pytest.approx(0.03, abs=1e-9)


# ===========================================================================
# Group 2: missing-data paths
# ===========================================================================


def test_turnover_ratio_missing_outstanding_is_all_nan() -> None:
    """``outstanding_share is None`` (column absent in data layer) →
    all-NaN Series; do NOT raise."""
    vol = pd.Series([1e6, 2e6, 3e6])
    ratio = turnover_ratio(vol, None)
    assert ratio.isna().all()
    # Length must still match the volume index so callers can
    # downstream-align with close / etc. without surprises.
    assert len(ratio) == len(vol)


def test_turnover_ratio_zero_outstanding_is_nan() -> None:
    """``outstanding == 0`` → divide-by-zero → NaN (not inf)."""
    vol = pd.Series([1e6])
    out = pd.Series([0.0])
    ratio = turnover_ratio(vol, out)
    assert pd.isna(ratio.iloc[0])
    assert not (ratio.abs() == float("inf")).any()


def test_turnover_ratio_nan_outstanding_is_nan() -> None:
    """``outstanding == NaN`` propagates to NaN (not 0 / NaN = NaN)."""
    vol = pd.Series([1e6])
    out = pd.Series([np.nan])
    ratio = turnover_ratio(vol, out)
    assert pd.isna(ratio.iloc[0])


def test_turnover_ratio_zero_volume_is_zero() -> None:
    """``volume == 0`` with valid outstanding → result = 0 (NOT NaN).

    Zero volume on a given bar is meaningful data (no trades that
    day); the factor library must not silently mask it as missing.
    """
    vol = pd.Series([0.0])
    out = pd.Series([1e10])
    ratio = turnover_ratio(vol, out)
    assert ratio.iloc[0] == 0.0


# ===========================================================================
# Group 3: mixed per-row cases
# ===========================================================================


def test_turnover_ratio_mixed_valid_and_invalid() -> None:
    """Per-row handling: valid / zero-out / nan-out / zero-vol all
    coexist in a single Series and produce the expected per-row
    result."""
    vol = pd.Series([1e6, 1e6, 1e6, 0.0])
    out = pd.Series([1e10, 0.0, np.nan, 1e10])
    ratio = turnover_ratio(vol, out)
    assert ratio.iloc[0] == pytest.approx(1e-4)
    assert pd.isna(ratio.iloc[1])
    assert pd.isna(ratio.iloc[2])
    assert ratio.iloc[3] == 0.0


# ===========================================================================
# Group 4: output contract
# ===========================================================================


def test_turnover_ratio_output_name() -> None:
    """Name is ``"turnover_ratio"`` regardless of which branch fired
    so pipelines can treat the column uniformly."""
    vol = pd.Series([1e6] * 3)
    out = pd.Series([1e10] * 3)
    assert turnover_ratio(vol, out).name == "turnover_ratio"
    # Also when outstanding is None.
    assert turnover_ratio(vol, None).name == "turnover_ratio"


def test_turnover_ratio_preserves_index() -> None:
    idx = pd.bdate_range(end=pd.Timestamp("2024-01-12"), periods=3)
    vol = pd.Series([1e6] * 3, index=idx)
    out = pd.Series([1e10] * 3, index=idx)
    ratio = turnover_ratio(vol, out)
    assert (ratio.index == idx).all()


# ===========================================================================
# Group 5: integration with conftest.make_bars(include_outstanding=True)
# ===========================================================================


def test_turnover_ratio_via_make_bars() -> None:
    """``make_bars(..., include_outstanding=True)`` round-trips."""
    df = make_bars([10.0] * 5, include_outstanding=True)
    ratio = turnover_ratio(df["volume"], df["outstanding_share"])
    assert ratio.name == "turnover_ratio"
    # All rows have a positive denominator → all positive ratios.
    assert (ratio > 0).all()
    # Magnitude sanity: 1e6 / 1e10 = 1e-4 = 0.0001.
    assert ratio.iloc[0] == pytest.approx(1e6 / 1e10)


def test_turnover_ratio_via_make_bars_without_outstanding() -> None:
    """``make_bars(..., include_outstanding=False)`` + None arg → NaN."""
    df = make_bars([10.0] * 5)
    assert "outstanding_share" not in df.columns
    ratio = turnover_ratio(df["volume"], None)
    assert ratio.isna().all()
