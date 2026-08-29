"""Tests for ``ops.quality.check_quality`` (W6.1.2).

Validates every issue kind the module can raise:

  * HARD: NaN in OHLCV, OHLC inconsistency, non-positive volume,
    duplicate dates, future dates, missing columns.
  * SOFT: outlier daily returns.
  * Clean df → no issues, ``has_hard_issues == False``.
  * Empty df → no issues (graceful no-op).
  * ``QualityReport.to_markdown`` output stable enough for 钉钉.

All tests run on synthetic frames so no network / DuckDB is
needed and ``pytest`` runs in < 1s.
"""

from __future__ import annotations

import sys
from datetime import date as date_cls
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ops.quality import (  # noqa: E402
    IssueSeverity,
    check_quality,
)


def _good_bars(n: int = 5) -> pd.DataFrame:
    """5-bar synthetic OHLCV with no defects. Each row is a
    business day going back from 2024-01-12."""
    dates = pd.bdate_range(end=pd.Timestamp("2024-01-12"), periods=n)
    closes = [10.00 + i * 0.10 for i in range(n)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": [c + 0.05 for c in closes],
            "low": [c - 0.05 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * n,
        }
    )


def test_check_quality_clean_df() -> None:
    """A clean df returns a report with zero issues."""
    df = _good_bars()
    report = check_quality(df, symbol="000001")
    assert report.symbol == "000001"
    assert report.n_rows == len(df)
    assert report.issues == []
    assert not report.has_hard_issues
    assert not report.has_soft_issues


def test_check_quality_empty_df_is_noop() -> None:
    """Empty df is a graceful no-op (some symbols legitimately
    produce zero rows on suspended days)."""
    df = _good_bars(n=0)
    report = check_quality(df, symbol="000001")
    assert report.n_rows == 0
    assert report.issues == []


def test_check_quality_hard_nan_in_close() -> None:
    """NaN in close → HARD issue."""
    df = _good_bars()
    df.loc[2, "close"] = float("nan")
    report = check_quality(df, symbol="000001")
    assert report.has_hard_issues
    nan_issues = [i for i in report.issues if i.kind == "NAN_CLOSE"]
    assert len(nan_issues) == 1
    assert nan_issues[0].severity == IssueSeverity.HARD


def test_check_quality_hard_ohlc_high_below_openclose() -> None:
    """high < max(open, close) → HARD issue."""
    df = _good_bars()
    df.loc[1, "high"] = df.loc[1, "low"]  # crush high to low
    report = check_quality(df, symbol="000001")
    assert report.has_hard_issues
    kinds = {i.kind for i in report.issues}
    assert "OHLC_HIGH_BELOW_OPENCLOSE" in kinds


def test_check_quality_hard_volume_non_positive() -> None:
    """volume == 0 → HARD issue (akshare uses 0 only for
    suspensions, which also produce NaN prices; surviving this
    check indicates a glitch)."""
    df = _good_bars()
    df.loc[0, "volume"] = 0.0
    report = check_quality(df, symbol="000001")
    assert report.has_hard_issues
    kinds = {i.kind for i in report.issues}
    assert "VOLUME_NON_POSITIVE" in kinds


def test_check_quality_hard_duplicate_date() -> None:
    """Same date appearing twice → HARD issue (would clobber on upsert)."""
    df = _good_bars(n=3)
    df.loc[2, "date"] = df.loc[1, "date"]  # duplicate
    report = check_quality(df, symbol="000001")
    assert report.has_hard_issues
    kinds = {i.kind for i in report.issues}
    assert "DUPLICATE_DATE" in kinds


def test_check_quality_hard_future_date() -> None:
    """A date > today is HARD (timezone drift near midnight)."""
    df = _good_bars(n=3)
    # Insert a future row at the end so it doesn't reorder other checks.
    future = pd.Timestamp(date_cls.today()) + pd.Timedelta(days=2)
    df.loc[len(df)] = {
        "date": future,
        "open": 11.0,
        "high": 11.05,
        "low": 10.95,
        "close": 11.0,
        "volume": 1_000_000.0,
    }
    report = check_quality(df, symbol="000001")
    assert report.has_hard_issues
    kinds = {i.kind for i in report.issues}
    assert "FUTURE_DATE" in kinds


def test_check_quality_hard_missing_columns() -> None:
    """Missing required columns → HARD, and row-scoped checks
    are skipped (else they'd KeyError on the missing cols)."""
    df = pd.DataFrame({"date": pd.bdate_range(end="2024-01-12", periods=3), "close": [1, 2, 3]})
    report = check_quality(df, symbol="000001")
    assert report.has_hard_issues
    assert any(i.kind == "MISSING_COLUMNS" for i in report.issues)


def test_check_quality_soft_outlier_return() -> None:
    """|daily_return| > 20% → SOFT issue, NOT hard (legitimate
    ChiNext moves exist)."""
    df = _good_bars(n=3)
    # Row 2 jumps 25% from row 1.
    df.loc[2, "close"] = df.loc[1, "close"] * 1.25
    df.loc[2, "open"] = df.loc[2, "close"]
    df.loc[2, "high"] = df.loc[2, "close"] + 0.05
    df.loc[2, "low"] = df.loc[2, "close"] - 0.05
    report = check_quality(df, symbol="000001")
    assert not report.has_hard_issues
    assert report.has_soft_issues
    assert any(i.kind == "OUTLIER_RETURN" for i in report.issues)


def test_check_quality_combined_hard_and_soft() -> None:
    """A df with both a HARD and a SOFT issue reports both; the
    HARD is what blocks upsert, the SOFT should still surface in
    the alert."""
    df = _good_bars(n=3)
    df.loc[0, "volume"] = 0.0  # HARD
    df.loc[2, "close"] = df.loc[1, "close"] * 1.25  # SOFT (outlier)
    df.loc[2, "open"] = df.loc[2, "close"]
    df.loc[2, "high"] = df.loc[2, "close"] + 0.05
    df.loc[2, "low"] = df.loc[2, "close"] - 0.05
    report = check_quality(df, symbol="000001")
    assert report.has_hard_issues
    assert report.has_soft_issues
    assert any(i.severity == IssueSeverity.HARD for i in report.issues)
    assert any(i.severity == IssueSeverity.SOFT for i in report.issues)


def test_quality_report_to_markdown() -> None:
    """``to_markdown`` includes the symbol header, HARD/SOFT counts,
    and one line per issue. Format is stable enough for 钉钉."""
    df = _good_bars()
    df.loc[0, "volume"] = 0.0
    report = check_quality(df, symbol="000001")
    md = report.to_markdown()
    assert "symbol=000001" in md
    assert "HARD=" in md
    assert "VOLUME_NON_POSITIVE" in md


def test_quality_report_to_markdown_clean() -> None:
    """``to_markdown`` of a clean report has 0 / 0 counts (no issue lines)."""
    report = check_quality(_good_bars(), symbol="000001")
    md = report.to_markdown()
    assert "HARD=0" in md
    assert "SOFT=0" in md


def test_quality_report_dataclass_is_frozen() -> None:
    """``QualityReport`` is frozen (so callers can't accidentally
    mutate the issue list and skip alerting)."""
    report = check_quality(_good_bars(), symbol="000001")
    with pytest.raises((AttributeError, Exception)):
        report.symbol = "mutated"  # type: ignore[misc]
