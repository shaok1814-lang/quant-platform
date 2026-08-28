"""W5 analytics module — performance, walk-forward, param sensitivity, optuna.

This package is the self-research walk-forward + optuna + performance
layer above AKQuant. AKQuant ships ``run_walk_forward`` (bar-count,
step-locked to test_period — does NOT satisfy CLAUDE.md "训练 2 年、
测试 1 年、季度滚动") and ``run_grid_search`` (CLAUDE.md bans grid
search). W5 wraps ``akquant.run_backtest`` directly per fold with a
pandas iterator and optuna.

Public surface (re-exported from sub-modules):

* ``research.factor_lib.analytics.performance`` — :func:`summarize_metrics`,
  :func:`oos_decay`, :func:`equity_curve`, :func:`daily_returns`,
  :data:`KEY_METRICS`.

* ``research.factor_lib.analytics.param_sensitivity`` — :func:`param_sensitivity_scan`,
  :func:`assert_stable`.

* ``research.factor_lib.analytics.optuna_runner`` — :func:`optimize_params`.

* ``research.factor_lib.analytics.walk_forward`` (W5-C4) — :func:`run_walk_forward`,
  :class:`FoldResult`, :class:`WalkForwardResult`.

Bound to CLAUDE.md 防过拟合原则:
  * IS / OOS explicit labeling (phase="is"/"oos" in every metrics dict).
  * step_months >= test_months guard (in walk_forward_splits).
  * Per-metric OOS decay assertion: higher-is-better >= 0.70, lower-is-better <= 1.30.
  * Optuna-only (no grid search).
  * Stability assertion: base_param ±20% range must hold (param_sensitivity.assert_stable).
"""

from __future__ import annotations

from research.factor_lib.analytics.optuna_runner import optimize_params
from research.factor_lib.analytics.param_sensitivity import (
    assert_stable,
    param_sensitivity_scan,
)
from research.factor_lib.analytics.performance import (
    KEY_METRICS,
    daily_returns,
    equity_curve,
    oos_decay,
    summarize_metrics,
)

__all__ = [
    "KEY_METRICS",
    "assert_stable",
    "daily_returns",
    "equity_curve",
    "oos_decay",
    "optimize_params",
    "param_sensitivity_scan",
    "summarize_metrics",
]
