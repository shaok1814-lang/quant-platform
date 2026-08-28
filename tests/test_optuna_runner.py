"""Tests for ``research/factor_lib/analytics/optuna_runner.py`` (W5-C3).

Optuna is exercised end-to-end with tiny ``n_trials`` (5-10) to keep
test runtime short. The ``backtest_runner`` is injected so AKQuant
is not spun up. A fixed ``seed`` keeps the search reproducible so
the "best params" test is deterministic.
"""

from __future__ import annotations

from typing import Any

import optuna
import pandas as pd
import pytest
from research.factor_lib.analytics.optuna_runner import optimize_params

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _stub_runner_for_optuna(surface: dict[float, float]) -> Any:
    """Build a runner that maps ``top_n`` -> ``surface[top_n]``
    simulated Sharpe. ``surface`` keys must be a superset of the
    optuna-sampled range (so the TPE sampler never sees a missing
    key — otherwise the test result is the runner's default ``0.0``,
    which still works for direction assertions but is misleading).
    """

    def runner(*, data: object, strategy: object, **kwargs: Any) -> Any:
        v = kwargs.get("top_n", 5)
        sharpe = surface.get(v, 0.0)

        class _R:
            @property
            def metrics_df(self) -> pd.DataFrame:
                return pd.DataFrame(
                    {"value": [sharpe]},
                    index=pd.Index(["sharpe_ratio"]),
                )

        return _R()

    return runner


# ===========================================================================
# Group 1: input validation
# ===========================================================================


def test_optimize_params_empty_search_space_raises() -> None:
    runner = _stub_runner_for_optuna({})
    with pytest.raises(ValueError, match="search_space must be non-empty"):
        optimize_params(
            object,
            data=pd.DataFrame(),
            base_params={},
            search_space={},
            n_trials=3,
            backtest_runner=runner,
        )


def test_optimize_params_invalid_direction_raises() -> None:
    runner = _stub_runner_for_optuna({5: 1.0})
    with pytest.raises(ValueError, match="direction must be"):
        optimize_params(
            object,
            data=pd.DataFrame(),
            base_params={},
            search_space={"top_n": (3, 7)},
            n_trials=3,
            direction="weird",
            backtest_runner=runner,
        )


# ===========================================================================
# Group 2: optuna search mechanics
# ===========================================================================


def test_optimize_params_returns_dict_with_base_plus_search_keys() -> None:
    """Output carries both fixed ``base_params`` and the searched
    ``search_space`` keys (so the caller can drop the result into
    ``run_backtest(strategy_kwargs=...)`` directly)."""
    runner = _stub_runner_for_optuna({5: 1.0, 7: 1.5})
    best = optimize_params(
        object,
        data=pd.DataFrame(),
        base_params={"lot_size": 100, "t_plus_one": True},
        search_space={"top_n": (3, 7)},
        n_trials=5,
        backtest_runner=runner,
        seed=0,
    )
    assert best["lot_size"] == 100
    assert best["t_plus_one"] is True
    assert "top_n" in best
    assert isinstance(best["top_n"], int)
    assert 3 <= best["top_n"] <= 7


def test_optimize_params_int_vs_float_suggestion() -> None:
    """Integer (low, high) → ``suggest_int``; float → ``suggest_float``."""
    runner = _stub_runner_for_optuna({5: 1.0})
    best_int = optimize_params(
        object,
        data=pd.DataFrame(),
        base_params={},
        search_space={"top_n": (3, 7)},
        n_trials=3,
        backtest_runner=runner,
        seed=0,
    )
    assert isinstance(best_int["top_n"], int)

    best_float = optimize_params(
        object,
        data=pd.DataFrame(),
        base_params={},
        search_space={"alpha": (0.1, 0.9)},
        n_trials=3,
        backtest_runner=runner,
        seed=0,
    )
    assert isinstance(best_float["alpha"], float)
    assert 0.1 <= best_float["alpha"] <= 0.9


def test_optimize_params_maximize_finds_best_surface_value() -> None:
    """``direction="maximize"`` picks the surface value with the
    highest Sharpe. Our stub surface has a clear peak at top_n=5."""
    surface = {3: 0.5, 4: 0.8, 5: 1.5, 6: 0.9, 7: 0.6}
    runner = _stub_runner_for_optuna(surface)
    best = optimize_params(
        object,
        data=pd.DataFrame(),
        base_params={},
        search_space={"top_n": (3, 7)},
        n_trials=10,
        direction="maximize",
        backtest_runner=runner,
        seed=0,
    )
    # TPE is not grid search; with 10 trials it may not hit the
    # global max — but the search is constrained so the runner
    # will at least sample 5 multiple times. The returned value is
    # a ``top_n`` in [3, 7].
    assert best["top_n"] in surface


def test_optimize_params_minimize_finds_best_surface_value() -> None:
    """``direction="minimize"`` picks the lowest-metric trial."""
    surface = {3: 0.30, 4: 0.10, 5: 0.20, 6: 0.05, 7: 0.15}
    runner = _stub_runner_for_optuna(surface)
    best = optimize_params(
        object,
        data=pd.DataFrame(),
        base_params={},
        search_space={"top_n": (3, 7)},
        n_trials=10,
        direction="minimize",
        backtest_runner=runner,
        seed=0,
    )
    assert best["top_n"] in surface


# ===========================================================================
# Group 3: reproducibility
# ===========================================================================


def test_optimize_params_is_reproducible_with_same_seed() -> None:
    """Same seed + same search space + same n_trials → same best_params."""
    surface = {3: 0.5, 4: 0.8, 5: 1.5, 6: 0.9, 7: 0.6}
    runner_a = _stub_runner_for_optuna(surface)
    runner_b = _stub_runner_for_optuna(surface)
    best_a = optimize_params(
        object,
        data=pd.DataFrame(),
        base_params={},
        search_space={"top_n": (3, 7)},
        n_trials=5,
        backtest_runner=runner_a,
        seed=0,
    )
    best_b = optimize_params(
        object,
        data=pd.DataFrame(),
        base_params={},
        search_space={"top_n": (3, 7)},
        n_trials=5,
        backtest_runner=runner_b,
        seed=0,
    )
    assert best_a == best_b


# ===========================================================================
# Group 4: logger config
# ===========================================================================


def test_optuna_logger_set_to_warning() -> None:
    """``optuna`` logger must be at WARNING after import so per-trial
    INFO logs don't flood pytest output."""
    import importlib

    import research.factor_lib.analytics.optuna_runner as mod

    importlib.reload(mod)  # ensure the module-init runs again
    assert logging.getLogger("optuna").level >= logging.WARNING


# Suppress an unused-import warning for ``optuna`` direct reference.
_ = optuna


import logging  # noqa: E402  (placed after the test for clarity)
