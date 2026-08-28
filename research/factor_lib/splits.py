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
    """Walk-forward rolling splits — N (train, test) pairs.

    For ``i = 0, 1, 2, ...``:
      * train window ``[start + i*step_months, start + i*step_months + train_months)``
      * test  window ``[start + i*step_months + train_months,
                          start + i*step_months + train_months + test_months)``

    The number of folds is the largest N such that the N-th test
    window still fits inside ``df``'s date range. Empty folds (zero
    rows in either window) are skipped — this happens when the data
    range is tight on a calendar-month boundary.

    Args:
        df: Bars DataFrame. Must carry a ``date`` column.
        train_months: Training window in months. Default ``24`` (2y).
        test_months: Test window in months. Default ``12`` (1y).
        step_months: Roll step in months. Default ``3`` (quarterly).
            Must be ``>= test_months`` (anti-overfit guard).

    Returns:
        ``list[(train_df, test_df)]`` of length N (typically > 1 for
        any realistic data range). Empty ``df`` → ``[]``.

    Raises:
        NotImplementedError: if ``step_months < test_months``
            (overlapping test folds = data leakage). Locked anti-overfit
            guard per CLAUDE.md 防过拟合原则.
        KeyError: if ``df`` does not contain a ``date`` column.

    Note:
        Boundary semantics:
          * Train end is inclusive (``DateOffset(days=-1)``).
          * Test start is the day after train end.
          * Test end is inclusive (``DateOffset(days=-1)``).
          * Each fold's slices are ``.copy()`` so downstream mutation
            does not leak back into ``df``.

        The function does NOT itself run a backtest — it only produces
        date-sliced frames. The caller (typically
        :func:`research.factor_lib.analytics.walk_forward.run_walk_forward`)
        invokes ``akquant.run_backtest`` per fold and aggregates the
        IS / OOS metrics.
    """
    if step_months < test_months:
        raise NotImplementedError(
            f"walk_forward_splits: step_months ({step_months}) must be >= "
            f"test_months ({test_months}). A shorter step creates overlapping "
            f"test folds (data leakage), which is the canonical 'fake "
            f"walk-forward' overfitting pattern that CLAUDE.md 防过拟合 "
            f"原则 explicitly bans."
        )
    if "date" not in df.columns:
        raise KeyError("walk_forward_splits requires a 'date' column in df")
    if df.empty:
        return []

    start = pd.Timestamp(pd.to_datetime(df["date"]).min())
    out: list[tuple[BarsDF, BarsDF]] = []
    # Safety cap so a typo (e.g. step_months=0) does not loop forever.
    max_folds = 1000
    for i in range(max_folds):
        train_start = start + pd.DateOffset(months=i * step_months)
        train_end = train_start + pd.DateOffset(months=train_months) - pd.DateOffset(days=1)
        test_start = train_end + pd.DateOffset(days=1)
        test_end = test_start + pd.DateOffset(months=test_months) - pd.DateOffset(days=1)
        last_data_date = pd.Timestamp(pd.to_datetime(df["date"]).max())
        if test_end > last_data_date:
            break
        train_mask = (pd.to_datetime(df["date"]) >= train_start) & (
            pd.to_datetime(df["date"]) <= train_end
        )
        test_mask = (pd.to_datetime(df["date"]) >= test_start) & (
            pd.to_datetime(df["date"]) <= test_end
        )
        train_df = df.loc[train_mask].copy()
        test_df = df.loc[test_mask].copy()
        # Skip empty folds (boundary effect); warn if BOTH empty (data too short).
        if len(train_df) == 0 and len(test_df) == 0:
            continue
        out.append((train_df, test_df))
    return out
