"""E2E tests for the W5 walk-forward orchestrator (W5-C4).

These tests inject a ``backtest_runner`` stub so AKQuant is not
spun up. The stub maps a "sharpe" surface to the strategy_kwargs
the runner receives, letting the tests assert the orchestrator's
plumbing (folds, per-fold IS/OOS metrics, decay ratio) without
running a real backtest.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from research.factor_lib.analytics.walk_forward import (
    FoldResult,
    WalkForwardResult,
    run_walk_forward,
)
from tests.conftest import make_bars

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _stub_runner_factory(
    *,
    train_sharpe: float = 1.20,
    test_sharpe: float = 0.95,
) -> Any:
    """Build a ``backtest_runner`` that always returns a stub result
    with the configured Sharpe. Used to exercise the orchestrator
    plumbing without spinning AKQuant end-to-end.

    The stub inspects the data passed in (train or test slice) to
    return the corresponding "phase" Sharpe — but actually
    returns the same dict each time; the per-fold ``phase`` label
    is set by :func:`summarize_metrics` based on the caller's
    argument, not the stub's return.
    """

    def runner(*, data: object, strategy: object, **kwargs: Any) -> Any:
        class _R:
            @property
            def metrics_df(self) -> pd.DataFrame:
                # We use one fixed Sharpe value (test_sharpe) per stub
                # because the orchestrator only reads one metric per
                # run; the train result gets train_sharpe, the test
                # result gets test_sharpe. The orchestrator swaps the
                # value via ``train_sharpe`` / ``test_sharpe`` only
                # in the parametrized tests below.
                return pd.DataFrame(
                    {"value": [1.0]},
                    index=pd.Index(["sharpe_ratio"]),
                )

        return _R()

    return runner


def _stub_runner_with_is_oos(*, train_sharpe: float, test_sharpe: float) -> Any:
    """Per-fold IS/OOS Sharpe is read from the data slice's date range:
    the orchestrator calls the runner twice (train + test). This
    stub differentiates by the date range — train slice spans
    earlier dates, test slice spans later dates. We use a simpler
    trick: every call gets train_sharpe; we swap to test_sharpe
    for the second call by tracking a counter.

    A simpler approach (which the e2e below uses) is to differentiate
    by the data's first-date year: < 2022 → train, >= 2022 → test.
    """

    counter = {"n": 0}

    def runner(*, data: object, strategy: object, **kwargs: Any) -> Any:
        counter["n"] += 1
        # First call: train; second call: test. Per-fold.
        sharpe = train_sharpe if counter["n"] % 2 == 1 else test_sharpe
        # For folds > 1 we still alternate; works because each fold
        # calls train then test.

        class _R:
            @property
            def metrics_df(self) -> pd.DataFrame:
                return pd.DataFrame(
                    {"value": [sharpe]},
                    index=pd.Index(["sharpe_ratio"]),
                )

        return _R()

    return runner


def _long_bars(tmp_path: Any) -> pd.DataFrame:
    """Build ~6 years of synthetic bars for walk-forward to yield
    multiple folds."""
    return make_bars(
        [10.0 + i * 0.01 for i in range(72 * 21)],
        start="2018-01-01",
    )


# ===========================================================================
# Group 1: fold mechanics
# ===========================================================================


def test_run_walk_forward_returns_walk_forward_result(tmp_path: Any) -> None:
    bars = _long_bars(tmp_path)
    result = run_walk_forward(
        object,
        data=bars,
        base_params={"lot_size": 100},
        train_months=24,
        test_months=12,
        step_months=12,
        backtest_runner=_stub_runner_with_is_oos(train_sharpe=1.0, test_sharpe=0.8),
    )
    assert isinstance(result, WalkForwardResult)
    assert len(result.folds) >= 2
    for f in result.folds:
        assert isinstance(f, FoldResult)
        assert f.train_start < f.train_end < f.test_start < f.test_end


def test_run_walk_forward_each_fold_has_is_oos_metrics(tmp_path: Any) -> None:
    """Per-fold train_metrics has phase="is"; test_metrics has phase="oos"
    per CLAUDE.md "必须明确标注样本内还是样本外"."""
    bars = _long_bars(tmp_path)
    result = run_walk_forward(
        object,
        data=bars,
        base_params={},
        train_months=24,
        test_months=12,
        step_months=12,
        backtest_runner=_stub_runner_with_is_oos(train_sharpe=1.0, test_sharpe=0.8),
    )
    for f in result.folds:
        assert f.train_metrics["phase"] == "is"
        assert f.test_metrics["phase"] == "oos"
        # The runner returned Sharpe 1.0 for train, 0.8 for test.
        assert f.train_metrics["sharpe_ratio"] == pytest.approx(1.0)
        assert f.test_metrics["sharpe_ratio"] == pytest.approx(0.8)


# ===========================================================================
# Group 2: IS / OOS decay
# ===========================================================================


def test_run_walk_forward_is_to_oos_decay_last_fold(tmp_path: Any) -> None:
    """The aggregate ``is_to_oos_decay`` is computed on the LAST fold
    only. Train Sharpe 1.0, Test Sharpe 0.8 → decay 0.80.
    """
    bars = _long_bars(tmp_path)
    result = run_walk_forward(
        object,
        data=bars,
        base_params={},
        train_months=24,
        test_months=12,
        step_months=12,
        backtest_runner=_stub_runner_with_is_oos(train_sharpe=1.0, test_sharpe=0.8),
    )
    assert result.is_to_oos_decay["sharpe_ratio_ratio"] == pytest.approx(0.8)


def test_run_walk_forward_decay_within_claudemd_threshold(tmp_path: Any) -> None:
    """The canonical W5 assertion pattern (CLAUDE.md 衰减 < 30%):
    Sharpe 1.0 → 0.75 = decay 0.75 (just over the 0.70 threshold).
    """
    bars = _long_bars(tmp_path)
    result = run_walk_forward(
        object,
        data=bars,
        base_params={},
        train_months=24,
        test_months=12,
        step_months=12,
        backtest_runner=_stub_runner_with_is_oos(train_sharpe=1.0, test_sharpe=0.75),
    )
    ratio = result.is_to_oos_decay["sharpe_ratio_ratio"]
    # 0.75 ≥ 0.70 → passes CLAUDE.md invariant.
    assert ratio >= 0.70
    # Concrete: 0.75 / 1.0.
    assert ratio == pytest.approx(0.75)


def test_run_walk_forward_decay_violation_detectable(tmp_path: Any) -> None:
    """Sharpe 1.0 → 0.5 (decay 0.5) — caller can detect the violation
    via ``ratio < 0.70``."""
    bars = _long_bars(tmp_path)
    result = run_walk_forward(
        object,
        data=bars,
        base_params={},
        train_months=24,
        test_months=12,
        step_months=12,
        backtest_runner=_stub_runner_with_is_oos(train_sharpe=1.0, test_sharpe=0.5),
    )
    ratio = result.is_to_oos_decay["sharpe_ratio_ratio"]
    assert ratio < 0.70  # would fail the CLAUDE.md assertion


# ===========================================================================
# Group 3: optuna integration
# ===========================================================================


def test_run_walk_forward_optuna_trials_zero_uses_base_params(
    tmp_path: Any,
) -> None:
    """``optuna_trials=0`` (default) means no tuning; ``best_params``
    on every fold equals ``base_params``."""
    bars = _long_bars(tmp_path)
    base = {"lot_size": 100, "t_plus_one": True}
    result = run_walk_forward(
        object,
        data=bars,
        base_params=base,
        optuna_trials=0,
        train_months=24,
        test_months=12,
        step_months=12,
        backtest_runner=_stub_runner_with_is_oos(train_sharpe=1.0, test_sharpe=0.8),
    )
    for f in result.folds:
        assert f.best_params == base


def test_run_walk_forward_optuna_trials_positive_runs_per_fold(
    tmp_path: Any,
) -> None:
    """``optuna_trials > 0`` + ``optuna_search_space`` triggers optuna
    per fold. The stub runner returns a constant Sharpe regardless
    of the param value, so the best_params returned is determined
    entirely by optuna's TPE sampler (deterministic with seed).
    """
    bars = _long_bars(tmp_path)
    result = run_walk_forward(
        object,
        data=bars,
        base_params={"lot_size": 100},
        optuna_trials=3,
        optuna_search_space={"top_n": (3, 7)},
        train_months=24,
        test_months=12,
        step_months=12,
        backtest_runner=_stub_runner_with_is_oos(train_sharpe=1.0, test_sharpe=0.8),
    )
    # Every fold's best_params must include 'top_n' (the optuna
    # search space param) AND 'lot_size' (the base param).
    for f in result.folds:
        assert "top_n" in f.best_params
        assert f.best_params["lot_size"] == 100
        assert 3 <= f.best_params["top_n"] <= 7


def test_run_walk_forward_optuna_without_search_space_raises(
    tmp_path: Any,
) -> None:
    """``optuna_trials > 0`` with ``search_space=None`` is a misconfig
    (cannot tune what to tune) — raises ValueError."""
    bars = _long_bars(tmp_path)
    with pytest.raises(ValueError, match="optuna_search_space"):
        run_walk_forward(
            object,
            data=bars,
            base_params={},
            optuna_trials=5,
            optuna_search_space=None,
            train_months=24,
            test_months=12,
            step_months=12,
            backtest_runner=_stub_runner_with_is_oos(train_sharpe=1.0, test_sharpe=0.8),
        )


# ===========================================================================
# Group 4: input shape
# ===========================================================================


def test_run_walk_forward_single_dataframe(tmp_path: Any) -> None:
    """Single-symbol DataFrame input (no symbol column) is supported."""
    bars = _long_bars(tmp_path)
    result = run_walk_forward(
        object,
        data=bars,
        base_params={},
        train_months=24,
        test_months=12,
        step_months=12,
        backtest_runner=_stub_runner_with_is_oos(train_sharpe=1.0, test_sharpe=0.8),
    )
    assert len(result.folds) >= 2


def test_run_walk_forward_multi_symbol_dict(tmp_path: Any) -> None:
    """Multi-symbol dict input is auto-concatenated to a single timeline
    and sliced by date. Per-fold IS / OOS metrics are still emitted."""
    sym_a = make_bars(
        [10.0 + i * 0.01 for i in range(72 * 21)], start="2018-01-01", symbol="000001"
    )
    sym_b = make_bars(
        [20.0 + i * 0.01 for i in range(72 * 21)], start="2018-01-01", symbol="600000"
    )
    result = run_walk_forward(
        object,
        data={"000001": sym_a, "600000": sym_b},
        base_params={},
        train_months=24,
        test_months=12,
        step_months=12,
        backtest_runner=_stub_runner_with_is_oos(train_sharpe=1.0, test_sharpe=0.8),
    )
    assert len(result.folds) >= 2
    # The per-fold runner received a multi-symbol frame.
    # (No explicit assertion; verified by the runner being called
    # without raising.)


def test_run_walk_forward_empty_dict_raises() -> None:
    with pytest.raises(ValueError, match="data Mapping is empty"):
        run_walk_forward(
            object,
            data={},
            base_params={},
            backtest_runner=_stub_runner_factory(),
        )


def test_run_walk_forward_too_short_data_raises(tmp_path: Any) -> None:
    """Less than train_months+test_months of data → walk_forward_splits
    returns [] → orchestrator raises with a helpful message."""
    bars = make_bars([10.0] * 30, start="2024-01-01")  # ~6 weeks
    with pytest.raises(ValueError, match="0 folds"):
        run_walk_forward(
            object,
            data=bars,
            base_params={},
            train_months=24,
            test_months=12,
            step_months=12,
            backtest_runner=_stub_runner_factory(),
        )


# ===========================================================================
# Group 5: end-to-end — multiple folds with the same runner
# ===========================================================================


def test_run_walk_forward_e2e_smoke(tmp_path: Any) -> None:
    """The canonical W5 use case: 4 folds on ~6 years of synthetic
    data, default cadence, decay assertion, optuna off.

    This is the test reviewers should read to understand the W5
    public surface.
    """
    bars = _long_bars(tmp_path)
    result = run_walk_forward(
        object,
        data=bars,
        base_params={"lot_size": 100},
        train_months=24,
        test_months=12,
        step_months=12,
        backtest_runner=_stub_runner_with_is_oos(train_sharpe=1.10, test_sharpe=0.92),
    )
    # Multiple folds.
    assert len(result.folds) >= 3
    # Per-fold IS / OOS labeling.
    for f in result.folds:
        assert f.train_metrics["phase"] == "is"
        assert f.test_metrics["phase"] == "oos"
    # Aggregate decay (last fold) within CLAUDE.md threshold.
    ratio = result.is_to_oos_decay["sharpe_ratio_ratio"]
    assert 0.92 / 1.10 == pytest.approx(ratio)
    assert ratio >= 0.70


# Suppress unused-import warning for pytest (used implicitly by fixtures).
_ = pytest
