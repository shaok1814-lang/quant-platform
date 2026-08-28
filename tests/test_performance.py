"""Tests for ``research/factor_lib/analytics/performance.py`` (W5-C2).

These tests do NOT spin up AKQuant (which is slow). They construct a
``BacktestResult``-like object with the minimum surface area W5
wraps (``metrics_df`` + ``equity_curve`` + ``daily_returns``).
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import pytest

# AKQuant may be slow to import on first call; the type stub is
# already loaded for type-check purposes. We import it lazily inside
# the test fixtures that need it.
if "akquant" in sys.modules:
    import akquant  # noqa: F401
from research.factor_lib.analytics.performance import (
    KEY_METRICS,
    daily_returns,
    equity_curve,
    oos_decay,
    summarize_metrics,
)

# ---------------------------------------------------------------------------
# Fixtures — minimal BacktestResult-shaped doubles (no full AKQuant run)
# ---------------------------------------------------------------------------


class _StubResult:
    """Minimum surface area: ``metrics_df`` + ``equity_curve`` + ``daily_returns``.

    Sufficient for the W5 ``performance`` module's read-only API. Do
    not pass this to AKQuant — it has no other backtest internals.
    """

    def __init__(
        self,
        metrics: dict[str, float],
        equity: pd.Series | None = None,
        returns: pd.Series | None = None,
    ) -> None:
        self._metrics = metrics
        self._equity = equity
        self._returns = returns

    @property
    def metrics_df(self) -> pd.DataFrame:
        return pd.DataFrame(
            {"value": list(self._metrics.values())},
            index=pd.Index(list(self._metrics.keys()), name="metric"),
        )

    @property
    def equity_curve(self) -> pd.Series:
        if self._equity is None:
            return pd.Series(dtype=float)
        return self._equity

    @property
    def daily_returns(self) -> pd.Series:
        if self._returns is None:
            return pd.Series(dtype=float)
        return self._returns


@pytest.fixture
def stub_is() -> _StubResult:
    return _StubResult(
        metrics={
            "sharpe_ratio": 1.20,
            "sortino_ratio": 1.50,
            "calmar_ratio": 0.90,
            "max_drawdown": 0.10,
            "total_return_pct": 25.0,
            "volatility": 0.18,
            "win_rate": 55.0,
            "exposure_time_pct": 80.0,
            "execution_count": 12.0,
            "closed_trade_count": 10.0,
        }
    )


@pytest.fixture
def stub_oos_typical() -> _StubResult:
    """IS→OOS typical: Sharpe 1.2 → 0.95 (decay 21%, < 30%); max DD 0.10 → 0.12 (worse)."""
    return _StubResult(
        metrics={
            "sharpe_ratio": 0.95,
            "sortino_ratio": 1.18,
            "calmar_ratio": 0.78,
            "max_drawdown": 0.12,
            "total_return_pct": 20.0,
            "volatility": 0.20,
            "win_rate": 50.0,
            "exposure_time_pct": 80.0,
            "execution_count": 6.0,
            "closed_trade_count": 5.0,
        }
    )


# ===========================================================================
# Group 1: summarize_metrics — phase label
# ===========================================================================


def test_summarize_metrics_phase_default_is(stub_is: object) -> None:
    out = summarize_metrics(stub_is)
    assert out["phase"] == "is"


def test_summarize_metrics_phase_explicit_oos(stub_oos_typical: object) -> None:
    out = summarize_metrics(stub_oos_typical, phase="oos")
    assert out["phase"] == "oos"


def test_summarize_metrics_rejects_invalid_phase(stub_is: object) -> None:
    with pytest.raises(ValueError, match="phase"):
        summarize_metrics(stub_is, phase="invalid")
    with pytest.raises(ValueError, match="phase"):
        summarize_metrics(stub_is, phase="")


# ===========================================================================
# Group 2: summarize_metrics — extracts the canonical subset
# ===========================================================================


def test_summarize_metrics_extracts_all_key_metrics(stub_is: object) -> None:
    out = summarize_metrics(stub_is)
    for k in KEY_METRICS:
        assert k in out, f"key metric {k!r} missing from summarize_metrics output"
        assert isinstance(out[k], float)


def test_summarize_metrics_extracts_wrapper_extras(stub_is: object) -> None:
    """execution_count + closed_trade_count are AKQuant wrapper extras
    that the Rust pre-computed set does NOT include — they live in
    result.py:425-495. W5 must surface them so callers can sanity-check
    fold activity."""
    out = summarize_metrics(stub_is)
    assert "execution_count" in out
    assert "closed_trade_count" in out
    assert out["execution_count"] == 12.0
    assert out["closed_trade_count"] == 10.0


def test_summarize_metrics_skips_nan_cells() -> None:
    """NaN-valued metric cells are omitted from the output (caller
    can .get(k) safely)."""
    stub = _StubResult(metrics={"sharpe_ratio": float("nan"), "win_rate": 50.0})
    out = summarize_metrics(stub)
    assert "sharpe_ratio" not in out
    assert out["win_rate"] == 50.0


def test_summarize_metrics_skips_missing_columns() -> None:
    """metrics_df may not contain every KEY_METRICS key (e.g. an AKQuant
    version without UPI). Missing keys are skipped, not raised."""
    stub = _StubResult(metrics={"sharpe_ratio": 1.0})
    out = summarize_metrics(stub)
    assert out["sharpe_ratio"] == 1.0
    assert "sortino_ratio" not in out
    assert out["phase"] == "is"


# ===========================================================================
# Group 3: oos_decay — per-metric ratio math
# ===========================================================================


def test_oos_decay_per_metric_ratio(stub_is: object, stub_oos_typical: object) -> None:
    is_metrics = summarize_metrics(stub_is)
    oos_metrics = summarize_metrics(stub_oos_typical, phase="oos")
    decay = oos_decay(is_metrics, oos_metrics)
    # sharpe 1.20 → 0.95 = 0.7917 (within CLAUDE.md decay < 30%).
    assert decay["sharpe_ratio_ratio"] == pytest.approx(0.95 / 1.20)
    # max_drawdown 0.10 → 0.12 = 1.2 (slightly worse, within budget).
    assert decay["max_drawdown_ratio"] == pytest.approx(0.12 / 0.10)
    # total_return_pct 25 → 20 = 0.8.
    assert decay["total_return_pct_ratio"] == pytest.approx(20.0 / 25.0)


def test_oos_decay_claudemd_decay_assertion_passes(
    stub_is: object, stub_oos_typical: object
) -> None:
    """The W5 decay assertion pattern: higher-is-better metrics >= 0.70,
    lower-is-better metrics <= 1.30."""
    is_metrics = summarize_metrics(stub_is)
    oos_metrics = summarize_metrics(stub_oos_typical, phase="oos")
    decay = oos_decay(is_metrics, oos_metrics)
    # Higher-is-better: Sharpe, Sortino, Calmar, total_return_pct, win_rate.
    for k in ("sharpe_ratio", "sortino_ratio", "calmar_ratio", "total_return_pct", "win_rate"):
        assert decay[f"{k}_ratio"] >= 0.70, (
            f"CLAUDE.md decay invariant violated: {k} OOS/IS = "
            f"{decay[f'{k}_ratio']:.3f} < 0.70 (a 30% drop is allowed)."
        )
    # Lower-is-better: max_drawdown, volatility.
    for k in ("max_drawdown", "volatility"):
        assert decay[f"{k}_ratio"] <= 1.30, (
            f"CLAUDE.md decay invariant violated: {k} OOS/IS = {decay[f'{k}_ratio']:.3f} > 1.30."
        )


def test_oos_decay_skips_phase_key() -> None:
    in_sample = {"phase": "is", "sharpe_ratio": 1.0}
    out_of_sample = {"phase": "oos", "sharpe_ratio": 0.8}
    decay = oos_decay(in_sample, out_of_sample)
    assert "phase_ratio" not in decay
    assert decay["sharpe_ratio_ratio"] == pytest.approx(0.8)


def test_oos_decay_skips_missing_key_in_oos() -> None:
    in_sample = {"sharpe_ratio": 1.0, "win_rate": 60.0}
    out_of_sample = {"sharpe_ratio": 0.8}  # win_rate missing
    decay = oos_decay(in_sample, out_of_sample)
    assert "sharpe_ratio_ratio" in decay
    assert "win_rate_ratio" not in decay


def test_oos_decay_skips_missing_key_in_is() -> None:
    in_sample = {"sharpe_ratio": 1.0}
    out_of_sample = {"sharpe_ratio": 0.8, "win_rate": 50.0}  # IS missing win_rate
    decay = oos_decay(in_sample, out_of_sample)
    assert "sharpe_ratio_ratio" in decay
    assert "win_rate_ratio" not in decay


def test_oos_decay_skips_nan_values() -> None:
    """NaN propagation is avoided by explicit skip."""
    in_sample = {"sharpe_ratio": float("nan"), "win_rate": 50.0}
    out_of_sample = {"sharpe_ratio": 0.8, "win_rate": 40.0}
    decay = oos_decay(in_sample, out_of_sample)
    assert "sharpe_ratio_ratio" not in decay
    assert decay["win_rate_ratio"] == pytest.approx(0.8)


def test_oos_decay_skips_zero_denominator() -> None:
    """``is_val == 0`` ⇒ divide-by-zero is undefined; skip the metric."""
    in_sample = {"sharpe_ratio": 0.0, "win_rate": 50.0}
    out_of_sample = {"sharpe_ratio": 0.5, "win_rate": 40.0}
    decay = oos_decay(in_sample, out_of_sample)
    assert "sharpe_ratio_ratio" not in decay
    assert decay["win_rate_ratio"] == pytest.approx(0.8)


def test_oos_decay_handles_negative_metric_values() -> None:
    """Negative IS metrics (e.g. negative Sharpe) still produce a
    meaningful ratio."""
    in_sample = {"sharpe_ratio": -0.5}
    out_of_sample = {"sharpe_ratio": -0.4}
    decay = oos_decay(in_sample, out_of_sample)
    assert decay["sharpe_ratio_ratio"] == pytest.approx(-0.4 / -0.5)


# ===========================================================================
# Group 4: wrappers — equity_curve + daily_returns passthrough
# ===========================================================================


def test_equity_curve_passthrough() -> None:
    idx = pd.date_range("2024-01-01", periods=5, freq="B")
    equity = pd.Series([1_000_000.0, 1_010_000.0, 1_020_000.0, 1_015_000.0, 1_025_000.0], index=idx)
    stub = _StubResult(metrics={}, equity=equity)
    out = equity_curve(stub)
    pd.testing.assert_series_equal(out, equity)


def test_daily_returns_passthrough() -> None:
    idx = pd.date_range("2024-01-01", periods=5, freq="B")
    rets = pd.Series([0.0, 0.01, 0.0099, -0.005, 0.0099], index=idx)
    stub = _StubResult(metrics={}, returns=rets)
    out = daily_returns(stub)
    pd.testing.assert_series_equal(out, rets)


def test_equity_curve_empty_returns_empty_series() -> None:
    stub = _StubResult(metrics={}, equity=pd.Series(dtype=float))
    out = equity_curve(stub)
    assert len(out) == 0


# ===========================================================================
# Group 5: KEY_METRICS constant
# ===========================================================================


def test_key_metrics_constant_includes_canonical_subset() -> None:
    """``KEY_METRICS`` must include the CLAUDE.md-cited subset."""
    assert "sharpe_ratio" in KEY_METRICS
    assert "max_drawdown" in KEY_METRICS
    assert "total_return_pct" in KEY_METRICS


def test_key_metrics_constant_is_tuple_immutable() -> None:
    """``KEY_METRICS`` is a tuple — callers cannot mutate the canonical
    subset (e.g. via ``.append``)."""
    assert isinstance(KEY_METRICS, tuple)
    with pytest.raises(AttributeError):
        KEY_METRICS.append = lambda *args, **kwargs: None  # type: ignore[attr-defined]


# Suppress the unused import for `np` (kept for future tests).
_ = np
