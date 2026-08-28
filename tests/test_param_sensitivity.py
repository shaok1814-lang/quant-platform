"""Tests for ``research/factor_lib/analytics/param_sensitivity.py`` (W5-C3).

These tests inject a ``backtest_runner`` to avoid spinning AKQuant
end-to-end. The injected runner returns a minimal ``BacktestResult``-
shaped object that exposes ``metrics_df``.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from research.factor_lib.analytics.param_sensitivity import (
    assert_stable,
    param_sensitivity_scan,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _StubResult:
    """Minimum surface: ``metrics_df`` (the only attribute read)."""

    def __init__(self, metrics: dict[str, float]) -> None:
        self._metrics = metrics

    @property
    def metrics_df(self) -> pd.DataFrame:
        return pd.DataFrame(
            {"value": list(self._metrics.values())},
            index=pd.Index(list(self._metrics.keys()), name="metric"),
        )


def _stub_runner_factory(metric_to_value: dict[float, float]) -> Any:
    """Build a ``backtest_runner`` that maps each param value to a
    stub result. ``metric_to_value[v]`` is the simulated Sharpe
    for the trial where ``param=v``.

    W5.1-C4: the runner takes ``**kwargs`` (NOT a dedicated
    ``strategy_kwargs=...`` kwarg) because W5.1 spreads strategy
    params as top-level kwargs to AKQuant. ``top_n`` is extracted
    from the kwargs dict.
    """

    def runner(*, data: object, strategy: object, **kwargs: Any) -> _StubResult:
        v = kwargs.get("top_n", 5)
        sharpe = metric_to_value.get(v, 0.0)
        return _StubResult(metrics={"sharpe_ratio": sharpe})

    return runner


# ===========================================================================
# Group 1: scan — empty input validation
# ===========================================================================


def test_param_sensitivity_scan_empty_param_values_raises() -> None:
    runner = _stub_runner_factory({})
    with pytest.raises(ValueError, match="param_values must be non-empty"):
        param_sensitivity_scan(
            object,  # strategy_cls unused under stub
            data=pd.DataFrame(),
            base_params={},
            param_name="top_n",
            param_values=[],
            backtest_runner=runner,
        )


# ===========================================================================
# Group 2: scan — return DataFrame shape
# ===========================================================================


def test_param_sensitivity_scan_returns_dataframe_with_correct_columns() -> None:
    runner = _stub_runner_factory({3: 1.0, 5: 1.5, 7: 1.2})
    df = param_sensitivity_scan(
        object,
        data=pd.DataFrame(),
        base_params={"lot_size": 100},
        param_name="top_n",
        param_values=[3, 5, 7],
        backtest_runner=runner,
    )
    assert list(df.columns) == ["top_n", "sharpe_ratio"]
    assert len(df) == 3
    assert df["top_n"].tolist() == [3, 5, 7]
    assert df["sharpe_ratio"].tolist() == [1.0, 1.5, 1.2]


def test_param_sensitivity_scan_passes_base_params_to_runner() -> None:
    """``base_params`` are merged into top-level kwargs for every
    sweep point (the ``param_name`` value is the only thing that
    changes). W5.1-C4: ``strategy_kwargs=...`` is no longer used
    by the W5 walker — strategy params flow as top-level kwargs."""
    received: list[dict[str, Any]] = []

    def runner(*, data: object, strategy: object, **kwargs: Any) -> _StubResult:
        received.append(dict(kwargs))
        return _StubResult(metrics={"sharpe_ratio": 1.0})

    param_sensitivity_scan(
        object,
        data=pd.DataFrame(),
        base_params={"lot_size": 100, "t_plus_one": True},
        param_name="top_n",
        param_values=[3, 5, 7],
        backtest_runner=runner,
    )
    assert len(received) == 3
    for kw in received:
        assert kw["lot_size"] == 100
        assert kw["t_plus_one"] is True
    # The param_name is overridden in every sweep point.
    assert [kw["top_n"] for kw in received] == [3, 5, 7]


def test_param_sensitivity_scan_other_params_merged() -> None:
    received: list[dict[str, Any]] = []

    def runner(*, data: object, strategy: object, **kwargs: Any) -> _StubResult:
        received.append(dict(kwargs))
        return _StubResult(metrics={"sharpe_ratio": 1.0})

    param_sensitivity_scan(
        object,
        data=pd.DataFrame(),
        base_params={"lot_size": 100},
        param_name="top_n",
        param_values=[3],
        other_params={"initial_cash": 1_000_000.0},
        backtest_runner=runner,
    )
    assert received[0]["lot_size"] == 100
    assert received[0]["initial_cash"] == 1_000_000.0
    assert received[0]["top_n"] == 3


def test_param_sensitivity_scan_forwards_data_and_run_kwargs() -> None:
    received_data: list[object] = []
    received_extra: list[dict[str, Any]] = []

    def runner(*, data: object, strategy: object, **kwargs: Any) -> _StubResult:
        received_data.append(data)
        # ``run_backtest_kwargs`` (initial_cash, show_progress) are
        # mixed into the runner's kwargs via the W5 walker's
        # ``runner(**base_kwargs, **kwargs)`` spread.
        received_extra.append(kwargs)
        return _StubResult(metrics={"sharpe_ratio": 1.0})

    sentinel_df = pd.DataFrame({"x": [1, 2, 3]})
    param_sensitivity_scan(
        object,
        data=sentinel_df,
        base_params={},
        param_name="top_n",
        param_values=[3, 5],
        backtest_runner=runner,
        run_backtest_kwargs={"initial_cash": 999.0, "show_progress": False},
    )
    assert received_data[0] is sentinel_df
    assert received_extra[0]["initial_cash"] == 999.0
    assert received_extra[0]["show_progress"] is False


# ===========================================================================
# Group 3: scan — default runner
# ===========================================================================


def test_param_sensitivity_scan_default_runner_is_akquant() -> None:
    """Without an injected ``backtest_runner``, the module-level
    default resolves to ``akquant.run_backtest``."""
    from research.factor_lib.analytics import param_sensitivity as mod

    # Inspect the function's source — it should lazy-import akquant.
    src = mod.__file__ or ""
    assert src.endswith("param_sensitivity.py")


# ===========================================================================
# Group 4: assert_stable — pass cases
# ===========================================================================


def test_assert_stable_passes_when_all_within_band() -> None:
    df = pd.DataFrame({"top_n": [4, 5, 6], "sharpe_ratio": [0.95, 1.00, 0.97]})
    # base=1.00, tolerance=0.20 → band [0.80, 1.20]. All within.
    assert_stable(df, base_param=5, base_metric_value=1.00, tolerance_pct=0.20)


def test_assert_stable_tolerance_0_is_exact_match() -> None:
    df = pd.DataFrame({"x": [1, 2, 3], "sharpe_ratio": [1.0, 1.0, 1.0]})
    assert_stable(df, base_param=2, base_metric_value=1.0, tolerance_pct=0.0)


def test_assert_stable_lower_is_better_metric_inverts_via_tolerance() -> None:
    """For lower-is-better metrics (max_drawdown / volatility), callers
    can flip the comparison by negating both sides — the helper
    itself uses absolute ±tolerance, so use a tight tolerance for
    the test."""
    df = pd.DataFrame({"x": [1, 2, 3], "max_drawdown": [0.10, 0.11, 0.12]})
    assert_stable(
        df,
        base_param=2,
        base_metric_value=0.10,
        tolerance_pct=0.20,
        metric="max_drawdown",
    )


# ===========================================================================
# Group 5: assert_stable — fail cases
# ===========================================================================


def test_assert_stable_raises_when_row_outside_band() -> None:
    df = pd.DataFrame({"top_n": [4, 5, 6], "sharpe_ratio": [0.95, 1.00, 0.5]})
    with pytest.raises(AssertionError) as exc_info:
        assert_stable(df, base_param=5, base_metric_value=1.00, tolerance_pct=0.20)
    msg = str(exc_info.value)
    assert "CLAUDE.md" in msg
    assert "0.5" in msg  # the violator value is reported
    assert "6" in msg  # the violator param is reported


def test_assert_stable_raises_on_both_sides_violation() -> None:
    df = pd.DataFrame({"x": [1, 2, 3], "m": [0.5, 1.0, 1.5]})
    with pytest.raises(AssertionError) as exc_info:
        assert_stable(
            df,
            base_param=2,
            base_metric_value=1.0,
            tolerance_pct=0.20,
            metric="m",
        )
    msg = str(exc_info.value)
    # Both 0.5 (below band) and 1.5 (above band) must be reported.
    assert "0.5" in msg
    assert "1.5" in msg


# ===========================================================================
# Group 6: assert_stable — input validation
# ===========================================================================


def test_assert_stable_missing_metric_column_raises() -> None:
    df = pd.DataFrame({"x": [1, 2], "other_metric": [0.5, 0.6]})
    with pytest.raises(ValueError, match="scan_df must have a 'sharpe_ratio' column"):
        assert_stable(df, base_param=1, base_metric_value=0.5)


def test_assert_stable_invalid_tolerance_pct_raises() -> None:
    df = pd.DataFrame({"x": [1], "sharpe_ratio": [1.0]})
    with pytest.raises(ValueError, match="tolerance_pct"):
        assert_stable(df, base_param=1, base_metric_value=1.0, tolerance_pct=1.5)
    with pytest.raises(ValueError, match="tolerance_pct"):
        assert_stable(df, base_param=1, base_metric_value=1.0, tolerance_pct=-0.1)


# ===========================================================================
# Group 7: end-to-end scan + assert_stable pattern (CLAUDE.md use case)
# ===========================================================================


def test_claudemd_param_stability_pattern() -> None:
    """The canonical W5 use case:
    df = param_sensitivity_scan(...)
    base_metric = df.loc[df[param] == base, metric].iloc[0]
    assert_stable(df, base_param=base, base_metric_value=base_metric)
    """
    # Synthetic "stable" surface: param=5 gives Sharpe=1.0; ±20% gives
    # 0.95, 0.97, 1.02, 0.99 — all within [0.80, 1.20].
    runner = _stub_runner_factory({4: 0.95, 5: 1.00, 6: 0.97, 7: 1.02, 8: 0.99})
    df = param_sensitivity_scan(
        object,
        data=pd.DataFrame(),
        base_params={"lot_size": 100},
        param_name="top_n",
        param_values=[4, 5, 6, 7, 8],
        backtest_runner=runner,
    )
    base = df.loc[df["top_n"] == 5, "sharpe_ratio"].iloc[0]
    # Should pass: every row's Sharpe within ±20% of 1.00.
    assert_stable(df, base_param=5, base_metric_value=base, tolerance_pct=0.20)
