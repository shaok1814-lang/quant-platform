"""Optuna parameter parameter (W5-C3).

CLAUDE.md 防过拟合原则 #4: "简单优先 — 同等收益后选择参数更的策略".
W5 ships optuna-driven single-objective optimization that callers
can plug into :func:`walk_forward.run_walk_forward` (``optuna_trials``
kwarg) or use standalone.

Note: this module is optuna-ONLY. CLAUDE.md "不要用 grid search"
explicitly forbids ``akquant.run_grid_search``. The optuna study
runs over the entire ``data`` (NOT a walk-forward train split) so
the caller is responsible for:

  * Calling :func:`walk_forward.run_walk_forward` separately for
    true IS / OOS validation.
  * Treating ``optimize_params`` output as "candidate best_params"
    only — never as a "verified" Sharpe per CLAUDE.md "禁止 在全样
    本上优化后直接报告 Sharpe".
"""

from __future__ import annotations

import logging
from typing import Any

import optuna
import pandas as pd

from research.factor_lib.analytics.param_sensitivity import BacktestRunner

__all__ = ["optimize_params"]

logger = logging.getLogger(__name__)

# Suppress optuna's per-trial INFO logs by default — they flood test
# output. Callers who want them can ``logging.getLogger("optuna")
# .setLevel(logging.INFO)``.
logging.getLogger("optuna").setLevel(logging.WARNING)


def _build_suggest_fn(trial: optuna.Trial, param_name: str, bounds: tuple[float, float]) -> Any:
    """Suggest a single ``param_name`` value within ``(low, high)``.

    Int vs float is inferred from the bounds type (both ints → int,
    else float). W5 only supports numeric params — categorical /
    discrete params require their own helper.
    """
    low, high = bounds
    if isinstance(low, int) and isinstance(high, int):
        return trial.suggest_int(param_name, low, high)
    return trial.suggest_float(param_name, float(low), float(high))


def optimize_params(
    strategy_cls: type,
    *,
    data: dict[str, pd.DataFrame] | pd.DataFrame,
    base_params: dict[str, Any],
    search_space: dict[str, tuple[float, float]],
    n_trials: int = 20,
    metric: str = "sharpe_ratio",
    direction: str = "maximize",
    run_backtest_kwargs: dict[str, Any] | None = None,
    backtest_runner: BacktestRunner | None = None,
    seed: int | None = 0,
) -> dict[str, Any]:
    """Optuna-driven search over ``search_space`` (low, high) per param.

    Args:
        strategy_cls: AKQuant ``Strategy`` subclass.
        data: ``Dict[str, pd.DataFrame]`` or ``pd.DataFrame``.
        base_params: ``strategy_kwargs`` baseline (the params NOT in
            ``search_space``).
        search_space: ``{param_name: (low, high)}``. Each value
            is suggested per trial; ``low/high`` type (int vs float)
            decides the suggestion mode.
        n_trials: Number of optuna trials. Default ``20``.
        metric: ``BacktestResult.metrics_df`` row to optimize.
        direction: ``"maximize"`` (default, for Sharpe / Sortino /
            total_return_pct) or ``"minimize"`` (for max_drawdown /
            volatility).
        run_backtest_kwargs: Extra kwargs forwarded to runner.
        backtest_runner: Injectable for tests. Defaults to
            :func:`akquant.run_backtest`.
        seed: Optuna sampler seed. Default ``0`` for reproducibility.

    Returns:
        ``dict[str, Any]`` — the best ``strategy_kwargs`` found by
        optuna. Includes both the searched (``search_space``) keys
        and the fixed (``base_params``) keys.

    Raises:
        ValueError: if ``search_space`` is empty, or ``direction`` is
            not in ``{"maximize", "minimize"}``.

    Note on metric direction:
        For ``max_drawdown`` and ``volatility`` (lower-is-better),
        use ``direction="minimize"``. The result is still the best
        trial's params; callers compare with ``oos_decay`` downstream.
    """
    if not search_space:
        raise ValueError("search_space must be non-empty")
    if direction not in ("maximize", "minimize"):
        raise ValueError(f"direction must be 'maximize' or 'minimize', got {direction!r}")

    from research.factor_lib.analytics.param_sensitivity import _default_runner

    runner = backtest_runner if backtest_runner is not None else _default_runner()
    base_kwargs = dict(run_backtest_kwargs or {})

    def objective(trial: optuna.Trial) -> float:
        kwargs: dict[str, Any] = dict(base_params)
        for param_name, bounds in search_space.items():
            kwargs[param_name] = _build_suggest_fn(trial, param_name, bounds)
        result = runner(
            data=data,
            strategy=strategy_cls,
            strategy_kwargs=kwargs,
            **base_kwargs,
        )
        return float(result.metrics_df.loc[metric, "value"])

    sampler = optuna.samplers.TPESampler(seed=seed) if seed is not None else None
    study = optuna.create_study(direction=direction, sampler=sampler)
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    best: dict[str, Any] = dict(base_params)
    best.update(study.best_params)
    logger.info(
        "optuna best %s=%s (n_trials=%d)",
        metric,
        study.best_value,
        n_trials,
    )
    return best
