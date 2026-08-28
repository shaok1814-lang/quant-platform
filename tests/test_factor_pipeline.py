"""Tests for ``research/factor_lib/pipeline.py`` and ``splits.py`` (W3.1-C7)."""

from __future__ import annotations

import pandas as pd
import pytest
from research.factor_lib import (
    LONG_FORMAT_COLUMNS,
    FactorPipeline,
    bollinger_z,
    ma_deviation,
    n_day_return,
    rsi,
    turnover_ratio,
)
from research.factor_lib.splits import time_split, walk_forward_splits
from tests.conftest import make_bars

# ===========================================================================
# Helpers
# ===========================================================================


def _df_with_multiindex(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Wrap a single-symbol bars frame in a ``(date, symbol)`` MultiIndex.

    Used to exercise the pipeline's multi-symbol cross-section
    grouping path.
    """
    out = df.copy()
    out["symbol"] = symbol
    return out.set_index(["date", "symbol"])


def _two_symbol_universe() -> pd.DataFrame:
    """Build a 60-bar two-symbol multi-index frame for pipeline tests."""
    sym_a = make_bars([10.0 + i * 0.05 for i in range(60)], symbol="000001")
    sym_b = make_bars([20.0 + i * 0.05 for i in range(60)], symbol="600000")
    merged = pd.concat([_df_with_multiindex(sym_a, "000001"), _df_with_multiindex(sym_b, "600000")])
    return merged


# ===========================================================================
# Group 1: FactorPipeline long-format output
# ===========================================================================


def test_pipeline_long_output_columns_single_symbol() -> None:
    """Single-symbol input → long output with the canonical columns."""
    df = make_bars([10.0 + i * 0.1 for i in range(40)])
    pipeline = FactorPipeline(
        factors=(("ma_dev_20", lambda d: ma_deviation(d["close"], bar_window=20)),),
        output_format="long",
    )
    out = pipeline.compute(df)
    # Long-format columns: date + (placeholder symbol) + factor_name + factor_value.
    assert set(out.columns) >= {"date", "factor_name", "factor_value"}


def test_pipeline_long_output_columns_multi_symbol() -> None:
    """Multi-symbol input → long output carries the symbol column."""
    universe = _two_symbol_universe()
    pipeline = FactorPipeline(
        factors=(("ma_dev_20", lambda d: ma_deviation(d["close"], bar_window=20)),),
        output_format="long",
    )
    out = pipeline.compute(universe)
    assert set(out.columns) == set(LONG_FORMAT_COLUMNS)
    # 2 symbols × 60 bars = 120 long rows.
    assert len(out) == 120
    # Each factor appears once per (date, symbol).
    assert out["factor_name"].unique().tolist() == ["ma_dev_20"]


# ===========================================================================
# Group 2: FactorPipeline wide-format output
# ===========================================================================


def test_pipeline_wide_output_multiindex() -> None:
    """Wide output keeps the ``(date, symbol)`` MultiIndex and one
    column per factor."""
    universe = _two_symbol_universe()
    pipeline = FactorPipeline(
        factors=(
            ("ma_dev_20", lambda d: ma_deviation(d["close"], bar_window=20)),
            ("nret_10", lambda d: n_day_return(d["close"], window=10)),
        ),
        output_format="wide",
    )
    out = pipeline.compute(universe)
    assert isinstance(out.index, pd.MultiIndex)
    assert out.index.names == ["date", "symbol"]
    assert list(out.columns) == ["ma_dev_20", "nret_10"]


def test_pipeline_factor_order_matches_factors_tuple() -> None:
    """Column order in wide output matches the order of the factors tuple."""
    universe = _two_symbol_universe()
    pipeline = FactorPipeline(
        factors=(
            ("z_first", lambda d: bollinger_z(d["close"])),
            ("a_second", lambda d: ma_deviation(d["close"], bar_window=20)),
        ),
        output_format="wide",
    )
    out = pipeline.compute(universe)
    assert list(out.columns) == ["z_first", "a_second"]


# ===========================================================================
# Group 3: post-processing order
# ===========================================================================


def test_pipeline_applies_winsorize_then_standardize() -> None:
    """Winsorize runs before standardize; z-scored output should have
    mean ~ 0 and std ~ 1 (with infinite outliers clipped to bound)."""
    universe = _two_symbol_universe()
    pipeline = FactorPipeline(
        factors=(("ma_dev_20", lambda d: ma_deviation(d["close"], bar_window=20)),),
        output_format="wide",
    )
    out = pipeline.compute(universe)
    # Per-date cross-section: each date's values are z-scored.
    for _date, group in out.groupby(level=0):
        finite = group["ma_dev_20"].dropna()
        if len(finite) >= 2:
            assert finite.mean() == pytest.approx(0.0, abs=1e-9)
            assert finite.std(ddof=0) == pytest.approx(1.0, abs=1e-9)


def test_pipeline_standardize_false_skips_zscore() -> None:
    """``standardize=False`` keeps winsorized but raw magnitudes."""
    universe = _two_symbol_universe()
    pipeline = FactorPipeline(
        factors=(("ma_dev_20", lambda d: ma_deviation(d["close"], bar_window=20)),),
        output_format="wide",
        standardize=False,
    )
    out = pipeline.compute(universe)
    # First valid value per date is not necessarily 0 (no z-score).
    finite_first = out["ma_dev_20"].dropna().iloc[0]
    # Should NOT be near zero (would indicate z-scoring happened).
    assert abs(finite_first) > 1e-6


# ===========================================================================
# Group 4: post-processing methods
# ===========================================================================


@pytest.mark.parametrize(
    "method",
    [
        pytest.param("3sigma", id="3sigma"),
        pytest.param("mad", id="mad"),
        pytest.param("quantile", id="quantile"),
    ],
)
def test_pipeline_winsorize_methods(method: str) -> None:
    """All three winsorize methods are accepted."""
    universe = _two_symbol_universe()
    pipeline = FactorPipeline(
        factors=(("ma_dev_20", lambda d: ma_deviation(d["close"], bar_window=20)),),
        output_format="wide",
        winsorize_method=method,  # type: ignore[arg-type]
    )
    # Should not raise.
    out = pipeline.compute(universe)
    assert "ma_dev_20" in out.columns


# ===========================================================================
# Group 5: validation + edge cases
# ===========================================================================


def test_pipeline_raises_on_missing_core_column() -> None:
    """Empty ``factors`` tuple is a valid no-op pipeline (returns
    the empty-output frame); a malformed df is rejected at validation."""
    df = make_bars([10.0] * 20).drop(columns=["close"])
    pipeline = FactorPipeline(
        factors=(("ma_dev_20", lambda d: ma_deviation(d["close"], bar_window=20)),),
    )
    with pytest.raises(Exception):  # MissingColumnError
        pipeline.compute(df)


def test_pipeline_empty_input_returns_empty_output() -> None:
    """Empty input → empty output in the requested format."""
    df = make_bars([10.0] * 5).iloc[:0]
    pipeline = FactorPipeline(
        factors=(("ma_dev_20", lambda d: ma_deviation(d["close"], bar_window=20)),),
        output_format="long",
    )
    out = pipeline.compute(df)
    assert out.empty


# ===========================================================================
# Group 6: multiple factors
# ===========================================================================


def test_pipeline_with_liquidity_factor() -> None:
    """Liquidity factor (volume / outstanding) round-trips through
    the pipeline alongside price-based factors."""
    df_a = make_bars([10.0 + i for i in range(40)], symbol="000001", include_outstanding=True)
    df_b = make_bars([20.0 + i for i in range(40)], symbol="600000", include_outstanding=True)
    universe = pd.concat([_df_with_multiindex(df_a, "000001"), _df_with_multiindex(df_b, "600000")])
    pipeline = FactorPipeline(
        factors=(
            ("ma_dev_20", lambda d: ma_deviation(d["close"], bar_window=20)),
            ("turnover_ratio", lambda d: turnover_ratio(d["volume"], d.get("outstanding_share"))),
        ),
        output_format="wide",
    )
    out = pipeline.compute(universe)
    assert list(out.columns) == ["ma_dev_20", "turnover_ratio"]
    # turnover_ratio should be > 0 on all rows (positive denominator).
    assert (out["turnover_ratio"].dropna() > 0).all()


# ===========================================================================
# Group 7: time_split
# ===========================================================================


def test_time_split_basic() -> None:
    df = make_bars([10.0 + i for i in range(50)])
    train, test = time_split(
        df,
        train=("2024-01-08", "2024-01-19"),  # ~10 bars
        test=("2024-01-22", "2024-02-02"),  # ~10 bars
    )
    assert not train.empty
    assert not test.empty
    assert (train["date"] <= test["date"].min()).all()


def test_time_split_inclusive_bounds() -> None:
    """Both endpoints are inclusive — bars on the boundary appear in
    the slice."""
    df = make_bars([10.0 + i for i in range(30)])
    train, test = time_split(
        df, train=("2024-01-08", "2024-01-12"), test=("2024-01-15", "2024-01-19")
    )
    assert (train["date"] >= pd.Timestamp("2024-01-08")).all()
    assert (train["date"] <= pd.Timestamp("2024-01-12")).all()
    assert (test["date"] >= pd.Timestamp("2024-01-15")).all()
    assert (test["date"] <= pd.Timestamp("2024-01-19")).all()


def test_time_split_missing_date_raises() -> None:
    df = make_bars([10.0] * 5).drop(columns=["date"])
    with pytest.raises(KeyError, match="date"):
        time_split(df, train=("2024-01-08", "2024-01-12"), test=("2024-01-15", "2024-01-19"))


# ===========================================================================
# Group 8: walk_forward_splits stub + anti-overfit guard
# ===========================================================================


def test_walk_forward_splits_returns_rolling_count() -> None:
    """W5 upgrade: walk_forward_splits is now a true rolling iterator.

    With ~1600 business days (~6.4 years) and step_months=12 the
    rolling iterator yields ≥ 3 (train, test) folds. (The original
    W3 assertion of "1 split" no longer holds — the stub has been
    upgraded to a real iterator.)
    """
    df = make_bars([10.0 + i * 0.1 for i in range(1600)])
    splits = walk_forward_splits(df, train_months=24, test_months=12, step_months=12)
    assert len(splits) >= 3
    for train, test in splits:
        assert not train.empty
        assert not test.empty


def test_walk_forward_splits_step_lt_test_raises() -> None:
    """Anti-overfit guard: ``step_months < test_months`` ⇒
    ``NotImplementedError`` (overlapping test folds = leakage)."""
    df = make_bars([10.0] * 100)
    with pytest.raises(NotImplementedError, match="overlapping"):
        walk_forward_splits(df, train_months=24, test_months=12, step_months=6)


def test_walk_forward_splits_step_eq_test_ok() -> None:
    """``step_months == test_months`` (non-overlapping step) is OK."""
    df = make_bars([10.0 + i * 0.1 for i in range(1600)])
    splits = walk_forward_splits(df, train_months=24, test_months=12, step_months=12)
    # True rolling yields ≥ 3 folds for ~6.4 years of data.
    assert len(splits) >= 3


def test_walk_forward_splits_empty_returns_empty_list() -> None:
    df = make_bars([10.0] * 5).iloc[:0]
    assert walk_forward_splits(df, step_months=12) == []


def test_walk_forward_splits_missing_date_raises() -> None:
    df = make_bars([10.0] * 5).drop(columns=["date"])
    with pytest.raises(KeyError, match="date"):
        walk_forward_splits(df, step_months=12)


# ===========================================================================
# Group 9: smoke — all four families + liquidity together via pipeline
# ===========================================================================


def test_pipeline_smoke_all_four_families() -> None:
    """All four factor families compose into one pipeline output."""
    df_a = make_bars([10.0 + i for i in range(60)], symbol="000001", include_outstanding=True)
    df_b = make_bars([20.0 + i for i in range(60)], symbol="600000", include_outstanding=True)
    universe = pd.concat([_df_with_multiindex(df_a, "000001"), _df_with_multiindex(df_b, "600000")])
    pipeline = FactorPipeline(
        factors=(
            ("ma_dev_20", lambda d: ma_deviation(d["close"], bar_window=20)),
            ("nret_20", lambda d: n_day_return(d["close"], window=20)),
            ("rsi_14", lambda d: rsi(d["close"], window=14)),
            ("boll_z_20", lambda d: bollinger_z(d["close"])),
            ("turnover_ratio", lambda d: turnover_ratio(d["volume"], d.get("outstanding_share"))),
        ),
        output_format="wide",
    )
    out = pipeline.compute(universe)
    assert list(out.columns) == ["ma_dev_20", "nret_20", "rsi_14", "boll_z_20", "turnover_ratio"]
    # All factors are finite (not all NaN) on at least some rows.
    for col in out.columns:
        assert out[col].notna().any()
