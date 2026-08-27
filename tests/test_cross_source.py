"""Unit tests for the cross-source validator (W2.2).

All tests are offline (no network). The end-to-end "akshare vs
baostock on 000001" smoke is in
``test_cross_source_smoke_000001`` and is skipped by default — opt
in manually once the akshare proxy issue is fixed or until you
point it at baostock-only.
"""

from __future__ import annotations

import pandas as pd
import pytest
from data_layer.ingestion.akshare_fetcher import (
    ADJUST_QFQ,
    fetch_daily_bars_with_fallback,
)
from data_layer.ingestion.baostock_fetcher import fetch_daily_bars
from data_layer.validation import ValidationReport, diff_sources, validate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bars(
    closes: list[float],
    *,
    fetcher: str,
    symbol: str = "000001",
    start: str = "2024-01-08",
) -> pd.DataFrame:
    """Build a minimal bars DataFrame with the given close series."""
    n = len(closes)
    dates = pd.bdate_range(end=pd.Timestamp("2024-01-12"), periods=n)
    df = pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": [c + 0.05 for c in closes],
            "low": [c - 0.05 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * n,
            "amount": [10_000_000.0] * n,
        }
    )
    df.attrs["fetcher"] = fetcher
    df.attrs["symbol"] = symbol
    df.attrs["adjust"] = "qfq"
    df.attrs["fetched_at"] = "2026-08-27T00:00:00+00:00"
    return df


# ===========================================================================
# Group 1: diff_sources unit tests
# ===========================================================================


def test_diff_identical_series_is_zero() -> None:
    closes = [10.0, 10.1, 10.2, 10.3, 10.4]
    a = _make_bars(closes, fetcher="akshare")
    b = _make_bars(closes, fetcher="baostock")
    diffs = diff_sources(a, b)
    assert len(diffs) == 5
    assert (diffs["abs_diff"] == 0.0).all()
    assert (diffs["pct_diff_bps"] == 0.0).all()
    assert diffs.attrs["fetcher_a"] == "akshare"
    assert diffs.attrs["fetcher_b"] == "baostock"


def test_diff_with_small_gap_computes_bps_correctly() -> None:
    a = _make_bars([100.0, 100.0, 100.0], fetcher="akshare")
    # 100.05 vs 100.00 → 5 bps
    b = _make_bars([100.05, 100.05, 100.05], fetcher="baostock")
    diffs = diff_sources(a, b)
    assert len(diffs) == 3
    # Use abs-diff against 5.0 because pytest.approx on a Series can be
    # version-flaky; element-wise abs comparison is bulletproof.
    assert (diffs["pct_diff_bps"].sub(5.0).abs() < 0.01).all()


def test_diff_inner_join_drops_non_overlapping_dates() -> None:
    # ``a`` lives on 2024-01-08..12; ``b`` lives on 2024-02-01..05.
    # Inner join should produce zero rows.
    a = _make_bars([10.0, 11.0, 12.0], fetcher="akshare")
    b_dates = pd.bdate_range(end=pd.Timestamp("2024-02-05"), periods=3)
    b = pd.DataFrame(
        {"date": b_dates, "close": [10.0, 11.0, 12.0]}
    )
    b.attrs["fetcher"] = "baostock"
    diffs = diff_sources(a, b)
    assert len(diffs) == 0
    assert diffs.attrs["fetcher_a"] == "akshare"
    assert diffs.attrs["fetcher_b"] == "baostock"


def test_diff_raises_on_missing_columns() -> None:
    a = _make_bars([10.0], fetcher="akshare").drop(columns=["close"])
    b = _make_bars([10.0], fetcher="baostock")
    with pytest.raises(ValueError, match="close"):
        diff_sources(a, b)


# ===========================================================================
# Group 2: validate (threshold + report shape)
# ===========================================================================


def test_validate_passes_when_within_threshold() -> None:
    a = _make_bars([100.0, 100.0], fetcher="akshare")
    b = _make_bars([100.03, 100.04], fetcher="baostock")  # < 5 bps
    report = validate(a, b, threshold_bps=10.0)
    assert report.passed is True
    assert report.n_overlap == 2
    assert report.n_diff_exceed_threshold == 0
    assert isinstance(report, ValidationReport)


def test_validate_fails_when_exceeds_threshold() -> None:
    a = _make_bars([100.0, 100.0, 100.0], fetcher="akshare")
    b = _make_bars([101.0, 101.0, 101.0], fetcher="baostock")  # ~100 bps
    report = validate(a, b, threshold_bps=50.0)
    assert report.passed is False
    assert report.n_diff_exceed_threshold == 3


def test_validate_empty_overlap_returns_zeroed_report() -> None:
    a = _make_bars([10.0, 11.0], fetcher="akshare")
    # Build b with no overlapping date — start 10 years later.
    b_dates = pd.bdate_range(start=pd.Timestamp("2034-01-01"), periods=2)
    b = pd.DataFrame(
        {"date": b_dates, "close": [10.0, 11.0]}
    )
    b.attrs["fetcher"] = "baostock"
    report = validate(a, b)
    assert report.n_overlap == 0
    assert report.passed is True  # vacuous pass on empty overlap
    assert report.max_pct_diff_bps == 0.0


def test_validate_echoes_fetcher_labels() -> None:
    a = _make_bars([10.0], fetcher="akshare")
    b = _make_bars([10.0], fetcher="baostock")
    report = validate(a, b)
    assert report.fetcher_a == "akshare"
    assert report.fetcher_b == "baostock"


# ===========================================================================
# Group 3: end-to-end smoke (network)
# ===========================================================================


@pytest.mark.skip(
    reason="network test — requires akshare and baostock access. Run "
    "manually with `uv run pytest tests/test_cross_source.py -k smoke` "
    "after opting in. Useful for W2.2 release sign-off."
)
def test_cross_source_smoke_000001() -> None:
    """Validate akshare (via fallback) vs baostock on 000001.SZ."""
    df_a = fetch_daily_bars_with_fallback(
        "000001", "2026-08-20", "2026-08-26", adjust=ADJUST_QFQ
    )
    df_b = fetch_daily_bars(
        "000001", "2026-08-20", "2026-08-26", adjust="qfq"
    )
    report = validate(df_a, df_b, threshold_bps=50.0)
    # Sanity: should overlap, with very small bps (qfq rounding).
    assert report.n_overlap >= 3
    assert report.max_pct_diff_bps < 50.0
