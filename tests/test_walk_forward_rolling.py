"""Tests for the upgraded ``walk_forward_splits`` rolling iterator (W5-C1)."""

from __future__ import annotations

import pandas as pd
import pytest
from research.factor_lib.splits import walk_forward_splits


def _make_bars(n_days: int, start: str = "2024-01-01") -> pd.DataFrame:
    """Build a synthetic bars frame of ``n_days`` business days.

    Each row has ``date`` only (no other columns) — splits.py does
    not care about OHLCV.
    """
    dates = pd.bdate_range(start=start, periods=n_days)
    return pd.DataFrame({"date": dates})


# ===========================================================================
# Group 1: rolling output
# ===========================================================================


def test_walk_forward_splits_returns_multiple_folds_on_long_data() -> None:
    """~7 years (1512 business days) → multiple folds.

    step_months=12, train=24, test=12 → 3 folds fit before the data
    ends (2023-10-17). The point is the iterator yields multiple
    non-overlapping folds — the exact count depends on calendar-month
    boundaries, so the assertion is calendar-robust (>= 3).
    """
    bars = _make_bars(72 * 21, start="2018-01-01")  # ~6 years
    splits = walk_forward_splits(bars, train_months=24, test_months=12, step_months=12)
    assert len(splits) >= 3
    assert len(splits) <= 20  # safety cap


def test_walk_forward_splits_fold_dates_are_monotonic() -> None:
    """Each fold's test window starts strictly after the previous fold's
    test window ends — the canonical walk-forward invariant (test
    sets never overlap).

    Train windows may overlap when ``step_months < train_months``
    (here step=12, train=24, so trains overlap by 12m); that is the
    standard rolling walk-forward semantics and is NOT a violation.
    The non-overlap invariant is on the TEST windows.
    """
    bars = _make_bars(72 * 21, start="2018-01-01")
    splits = walk_forward_splits(bars, train_months=24, test_months=12, step_months=12)
    assert len(splits) >= 2
    for i in range(len(splits) - 1):
        _train_df_i, test_df_i = splits[i]
        _train_df_next, test_df_next = splits[i + 1]
        test_end_i = pd.Timestamp(pd.to_datetime(test_df_i["date"]).max())
        test_start_next = pd.Timestamp(pd.to_datetime(test_df_next["date"]).min())
        assert test_end_i < test_start_next, (
            f"Fold {i} test_end ({test_end_i}) overlaps fold {i + 1} "
            f"test_start ({test_start_next}) — test windows must not "
            f"overlap (anti-leakage invariant)."
        )


def test_walk_forward_splits_respects_train_months_window() -> None:
    """Each fold's train window spans approximately train_months months."""
    bars = _make_bars(60 * 21, start="2018-01-01")
    splits = walk_forward_splits(bars, train_months=24, test_months=12, step_months=12)
    assert splits  # non-empty
    train_df, _ = splits[0]
    span = (
        pd.Timestamp(pd.to_datetime(train_df["date"]).max())
        - pd.Timestamp(pd.to_datetime(train_df["date"]).min())
    ).days
    # 24 calendar months ≈ 730 days; allow 720-740 for safety.
    assert 720 <= span <= 740


def test_walk_forward_splits_respects_step_months() -> None:
    """Consecutive folds' train-start differ by approximately step_months."""
    bars = _make_bars(60 * 21, start="2018-01-01")
    splits = walk_forward_splits(bars, train_months=12, test_months=12, step_months=12)
    assert len(splits) >= 2
    for i in range(len(splits) - 1):
        train_i = pd.Timestamp(pd.to_datetime(splits[i][0]["date"]).min())
        train_next = pd.Timestamp(pd.to_datetime(splits[i + 1][0]["date"]).min())
        diff_days = (train_next - train_i).days
        # step_months=12 ≈ 365 days; allow 360-370.
        assert 360 <= diff_days <= 370


def test_walk_forward_splits_first_fold_uses_data_start() -> None:
    """Fold 0's train starts at the first row's date (no warmup data wasted)."""
    bars = _make_bars(60 * 21, start="2018-06-15")
    splits = walk_forward_splits(bars, train_months=12, test_months=12, step_months=12)
    assert splits
    first_train_start = pd.Timestamp(pd.to_datetime(splits[0][0]["date"]).min())
    assert first_train_start == pd.Timestamp("2018-06-15")


# ===========================================================================
# Group 2: anti-overfit guard (preserved from W3)
# ===========================================================================


def test_walk_forward_splits_step_lt_test_raises() -> None:
    """``step_months < test_months`` ⇒ ``NotImplementedError`` (overlapping
    folds = leakage). Locked anti-overfit guard per CLAUDE.md 防过拟合."""
    bars = _make_bars(60 * 21, start="2018-01-01")
    with pytest.raises(NotImplementedError, match="overlapping"):
        walk_forward_splits(bars, train_months=24, test_months=12, step_months=6)


def test_walk_forward_splits_step_eq_test_ok() -> None:
    """``step_months == test_months`` (non-overlapping) is allowed."""
    bars = _make_bars(60 * 21, start="2018-01-01")
    splits = walk_forward_splits(bars, train_months=24, test_months=12, step_months=12)
    assert len(splits) >= 2


def test_walk_forward_splits_step_gt_test_ok() -> None:
    """``step_months > test_months`` is allowed (gaps between test folds)."""
    bars = _make_bars(120 * 21, start="2018-01-01")
    splits = walk_forward_splits(bars, train_months=24, test_months=12, step_months=24)
    # step=24, train=24, test=12 → 1 fold (no overlap).
    assert len(splits) >= 1


# ===========================================================================
# Group 3: validation
# ===========================================================================


def test_walk_forward_splits_empty_returns_empty_list() -> None:
    bars = _make_bars(0)
    assert walk_forward_splits(bars, train_months=24, test_months=12, step_months=12) == []


def test_walk_forward_splits_missing_date_raises() -> None:
    bars = pd.DataFrame({"foo": [1, 2, 3]})
    with pytest.raises(KeyError, match="date"):
        walk_forward_splits(bars, train_months=24, test_months=12, step_months=12)


def test_walk_forward_splits_too_short_returns_empty() -> None:
    """Less than train_months+test_months of data → no folds fit."""
    bars = _make_bars(6 * 21, start="2024-01-01")  # ~6 months
    splits = walk_forward_splits(bars, train_months=24, test_months=12, step_months=12)
    assert splits == []


# ===========================================================================
# Group 4: contracts
# ===========================================================================


def test_walk_forward_splits_returns_copies_not_views() -> None:
    """Each (train_df, test_df) is a copy — caller mutation does not leak
    back into the source ``df``."""
    bars = _make_bars(60 * 21, start="2018-01-01")
    splits = walk_forward_splits(bars, train_months=24, test_months=12, step_months=12)
    assert splits
    train_df, _ = splits[0]
    n_before = len(train_df)
    train_df["__mutated__"] = True
    # The source ``bars`` must not have the column.
    assert "__mutated__" not in bars.columns
    # Re-running the iterator must not pick up the mutation either.
    splits2 = walk_forward_splits(bars, train_months=24, test_months=12, step_months=12)
    assert splits2
    train2, _ = splits2[0]
    assert "__mutated__" not in train2.columns
    assert len(train2) == n_before


def test_walk_forward_splits_step_huge_yields_at_most_one_fold() -> None:
    """``step_months=500`` (far larger than data range) → fold 0 still
    fits (its test window is within the data); fold 1+ breaks out
    because its test window exceeds the data. So the iterator
    yields exactly 1 fold before the loop exits cleanly (no infinite
    loop).
    """
    bars = _make_bars(60 * 21, start="2018-01-01")
    splits = walk_forward_splits(bars, train_months=24, test_months=12, step_months=500)
    assert len(splits) <= 1
