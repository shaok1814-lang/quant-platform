"""Train/test split helpers (W3 stub for W5 walk-forward).

W3 ships a single ``time_split`` plus a ``walk_forward_splits``
*stub* that returns one (first-window) split. The real walk-forward
rolling iterator — quarterly step, 24m train / 12m test, optuna
parameter search integration via ``akquant.ml.ValidationConfig`` —
lands in W5.

The stub deliberately rejects misuse that would silently understate
overfitting risk: a ``step_months`` strictly less than ``test_months``
would create overlapping test windows (data leakage across folds).
We raise ``NotImplementedError`` with an explicit anti-overfit
rationale so a caller cannot accidentally benchmark a leaky
walk-forward setup.
"""

from __future__ import annotations

import pandas as pd

from research.factor_lib._types import BarsDF

__all__ = ["SplitSpec", "time_split", "walk_forward_splits"]

# Inclusive date-range spec. ``SplitSpec(start_iso, end_iso)``. The
# ``walk_forward_splits`` helper also uses this type when serializing
# the inner (train, test) windows.
SplitSpec = tuple[str, str]


def time_split(
    df: BarsDF,
    *,
    train: SplitSpec,
    test: SplitSpec,
) -> tuple[BarsDF, BarsDF]:
    """Split ``df`` by inclusive date range into train / test slices.

    Each slice is a copy (so caller-side mutations do not leak into
    the input frame). The split is deterministic given the same
    inputs; ties on the boundary date are inclusive on both sides.

    Args:
        df: Bars DataFrame. Must carry a ``date`` column.
        train: ``(start_iso, end_iso)`` inclusive date range for
            the training window.
        test: ``(start_iso, end_iso)`` inclusive date range for
            the test window. May overlap with ``train`` (callers
            who want non-overlapping windows should pin ``test[0]``
            > ``train[1]``).

    Returns:
        ``(train_df, test_df)`` — both ``pd.DataFrame`` slices of
        ``df``. Empty slice (no bars in range) returns a copy of
        ``df`` with zero rows so the type contract holds.

    Raises:
        KeyError: if ``df`` does not contain a ``date`` column.
    """
    if "date" not in df.columns:
        raise KeyError("time_split requires a 'date' column in df")
    train_start = pd.Timestamp(train[0])
    train_end = pd.Timestamp(train[1])
    test_start = pd.Timestamp(test[0])
    test_end = pd.Timestamp(test[1])
    train_mask = (df["date"] >= train_start) & (df["date"] <= train_end)
    test_mask = (df["date"] >= test_start) & (df["date"] <= test_end)
    return df.loc[train_mask].copy(), df.loc[test_mask].copy()


def walk_forward_splits(
    df: BarsDF,
    *,
    train_months: int = 24,
    test_months: int = 12,
    step_months: int = 3,
) -> list[tuple[BarsDF, BarsDF]]:
    """W5 STUB: walk-forward rolling splits.

    Returns a single-element list ``[(first_train, first_test)]`` from
    ``time_split`` covering the first ``train_months`` of ``df`` as
    training and the next ``test_months`` as test. W5 will replace
    this with a full rolling iterator once
    ``akquant.ml.ValidationConfig`` is integrated.

    Anti-overfit guard:
        ``step_months < test_months`` is rejected with
        ``NotImplementedError``. A step shorter than the test window
        creates *overlapping* test folds (data leakage) — the
        canonical "fake walk-forward" setup that historically
        inflates in-sample Sharpe by 30-50%.

    Args:
        df: Bars DataFrame. Must carry a ``date`` column.
        train_months: Training window in months. Default ``24`` (2
            years) per CLAUDE.md anti-overfit policy.
        test_months: Test window in months. Default ``12`` (1 year).
        step_months: Roll step in months. Default ``3`` (quarterly).

    Returns:
        A single-element list ``[(train_df, test_df)]``. W5 will
        return one element per fold.

    Raises:
        NotImplementedError: if ``step_months < test_months``
            (anti-overfit guard).
        KeyError: if ``df`` does not contain a ``date`` column.
    """
    if step_months < test_months:
        raise NotImplementedError(
            f"walk_forward_splits: step_months ({step_months}) must be >= "
            f"test_months ({test_months}). A shorter step creates overlapping "
            f"test folds (data leakage), which is the canonical 'fake "
            f"walk-forward' overfitting pattern. W5 will integrate the real "
            f"rolling iterator once akquant.ml.ValidationConfig is wired; "
            f"for now use a non-overlapping step (step_months >= test_months) "
            f"or rely on the single-split return value."
        )
    if "date" not in df.columns:
        raise KeyError("walk_forward_splits requires a 'date' column in df")
    if df.empty:
        return []
    start = pd.Timestamp(df["date"].iloc[0])
    train_end = start + pd.DateOffset(months=train_months) - pd.DateOffset(days=1)
    test_start = train_end + pd.DateOffset(days=1)
    test_end = test_start + pd.DateOffset(months=test_months) - pd.DateOffset(days=1)
    return [
        time_split(
            df,
            train=(start.strftime("%Y-%m-%d"), train_end.strftime("%Y-%m-%d")),
            test=(test_start.strftime("%Y-%m-%d"), test_end.strftime("%Y-%m-%d")),
        )
    ]
