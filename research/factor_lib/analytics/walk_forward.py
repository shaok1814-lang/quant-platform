"""Walk-forward orchestrator (W5-C4).

Composes :mod:`research.factor_lib.splits` +
:mod:`research.factor_lib.analytics.performance` + (optionally)
:mod:`research.factor_lib.analytics.optuna_runner` to run a true
month-based walk-forward with per-fold optuna tuning and explicit
IS / OOS metric labeling.

This is W5's answer to:

  * CLAUDE.md 防过拟合原则 #1 (Walk-Forward 验证: 训练 2 年、测试
    1 年、季度滚动) — ``train_months=24, test_months=12,
    step_months=3`` defaults match.
  * CLAUDE.md 防过拟合原则 #2 (样本内外对比: 测试集表现表现衰减
    < 30%) — :data:`WalkForwardResult.is_to_oos_decay` exposes
    per-metric ratios; caller pattern:

        result = run_walk_forward(...)
        assert result.is_to_oos_decay["sharpe_ratio_ratio"] >= 0.70
  * CLAUDE.md "backtest-result conversations must tag every metric
    as in-sample or out-of-sample" — :func:`summarize_metrics`
    (phase="is" / "oos") is called per fold and surfaces the label
    in :attr:`FoldResult.train_metrics` /
    :attr:`FoldResult.test_metrics`.

AKQuant's own ``run_walk_forward`` (bar-count, step-locked to
test_period, grid-search under the hood) is intentionally NOT
used — it cannot satisfy CLAUDE.md "训练 2 年、测试 1 年、季度滚动"
without bypass. W5 wraps ``akquant.run_backtest`` directly per fold.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

from research.factor_lib.analytics.optuna_runner import optimize_params
from research.factor_lib.analytics.param_sensitivity import BacktestRunner
from research.factor_lib.analytics.performance import (
    oos_decay,
    summarize_metrics,
)
from research.factor_lib.splits import walk_forward_splits

if TYPE_CHECKING:
    pass

__all__ = ["FoldResult", "WalkForwardResult", "run_walk_forward"]


def _default_runner() -> BacktestRunner:
    import akquant

    return akquant.run_backtest


@dataclass(frozen=True)
class FoldResult:
    """Per-fold results from a walk-forward run.

    Attributes:
        fold_index: 0-based fold ordinal.
        train_start / train_end: Inclusive date range fed to the
            in-sample backtest (and the optuna search, if enabled).
        test_start / test_end: Inclusive date range fed to the
            out-of-sample backtest.
        train_metrics: Output of :func:`summarize_metrics` with
            ``phase="is"``. Includes a ``"phase"`` key per
            CLAUDE.md "必须明确标注样本内还是样本外".
        test_metrics: Output of :func:`summarize_metrics` with
            ``phase="oos"``.
        best_params: ``strategy_kwargs`` used for both train and
            test in this fold. Equals ``base_params`` when
            ``optuna_trials=0``; equals optuna's best otherwise.
    """

    fold_index: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_metrics: dict[str, float | str]
    test_metrics: dict[str, float | str]
    best_params: dict[str, Any]


@dataclass(frozen=True)
class WalkForwardResult:
    """Aggregate walk-forward result.

    Attributes:
        folds: Per-fold :class:`FoldResult` list, in chronological
            order.
        base_params: The ``strategy_kwargs`` baseline passed in (the
            optuna-free defaults).
        metric: Name of the optimized metric (default
            ``"sharpe_ratio"``).
        is_to_oos_decay: Per-metric OOS/IS ratio from
            :func:`oos_decay` applied to the **last fold**'s
            train/test metrics. (A multi-fold aggregated decay
            would need weighting by fold length / equity; W5 leaves
            that to the caller.)
    """

    folds: list[FoldResult]
    base_params: dict[str, Any]
    metric: str
    is_to_oos_decay: dict[str, float] = field(default_factory=dict)


def run_walk_forward(
    strategy_cls: type,
    *,
    data: pd.DataFrame | Mapping[str, pd.DataFrame],
    base_params: dict[str, Any],
    train_months: int = 24,
    test_months: int = 12,
    step_months: int = 3,
    optuna_trials: int = 0,
    optuna_search_space: Mapping[str, tuple[float, float]] | None = None,
    metric: str = "sharpe_ratio",
    run_backtest_kwargs: dict[str, Any] | None = None,
    backtest_runner: BacktestRunner | None = None,
) -> WalkForwardResult:
    """Run a month-based walk-forward over ``data``.

    For each fold in :func:`walk_forward_splits` (rolling iterator,
    ``step_months >= test_months`` anti-overfit guard):

      1. If ``optuna_trials > 0`` and ``optuna_search_space`` is
         provided, run :func:`optimize_params` on the fold's
         **train** slice to find ``best_params``.
      2. Otherwise, ``best_params = base_params`` (no tuning).
      3. Run the AKQuant backtest (or the injected ``backtest_runner``)
         on the train slice → :func:`summarize_metrics(phase="is")`.
      4. Run the same backtest on the test slice → :func:`summarize_metrics(phase="oos")`.
      5. Record the per-fold :class:`FoldResult`.

    Args:
        strategy_cls: AKQuant ``Strategy`` subclass.
        data: Bars frame(s). Either a single :class:`pd.DataFrame`
            (with a ``date`` column) or a
            ``Mapping[str, pd.DataFrame]`` (multi-symbol universe).
            The data's date column drives the split; multi-symbol
            frames are sliced as a whole (so the universe stays
            consistent across folds).
        base_params: ``strategy_kwargs`` baseline. Used unchanged
            when ``optuna_trials == 0``; serves as the optuna
            baseline otherwise.
        train_months: Forwarded to :func:`walk_forward_splits` —
            train window in months.
        test_months: Forwarded to :func:`walk_forward_splits` —
            test window in months.
        step_months: Forwarded to :func:`walk_forward_splits` —
            step size in months. :func:`walk_forward_splits`
            enforces the anti-overfit ``step_months >= test_months``
            guard.
        optuna_trials: If ``> 0`` and ``optuna_search_space`` is
            provided, optuna tunes per fold on the train slice.
            Default ``0`` (CLAUDE.md 防过拟合 #4: "简单优先" — no
            tuning unless explicitly requested).
        optuna_search_space: ``{param_name: (low, high)}`` for
            optuna's TPE sampler.
        metric: ``BacktestResult.metrics_df`` row to read.
        run_backtest_kwargs: Extra kwargs forwarded to the runner.
        backtest_runner: Optional injectable runner (tests inject
            this to avoid AKQuant spin-up). Defaults to
            :func:`akquant.run_backtest`.

    Returns:
        :class:`WalkForwardResult` with per-fold :class:`FoldResult`
        list + ``is_to_oos_decay`` computed on the last fold.

    Raises:
        ValueError: if the data has no ``date`` column or is empty
            after splitting.
    """
    runner = backtest_runner if backtest_runner is not None else _default_runner()
    base_kwargs = dict(run_backtest_kwargs or {})

    if isinstance(data, pd.DataFrame):
        bars = data
    else:
        # Multi-symbol universe: concat all symbol frames, sort by date.
        # This makes walk_forward_splits see a single timeline.
        if not data:
            raise ValueError("data Mapping is empty")
        bars = pd.concat(list(data.values()), ignore_index=True)
        bars = bars.sort_values("date").reset_index(drop=True)

    splits = walk_forward_splits(
        bars,
        train_months=train_months,
        test_months=test_months,
        step_months=step_months,
    )
    if not splits:
        raise ValueError(
            f"walk_forward_splits returned 0 folds (data range too short: "
            f"{bars['date'].min()} to {bars['date'].max()}; need at least "
            f"{train_months + test_months} months for one fold)"
        )

    folds: list[FoldResult] = []
    for fold_idx, (train_df, test_df) in enumerate(splits):
        # 1. Optionally tune on the train slice.
        if optuna_trials > 0:
            if optuna_search_space is None:
                raise ValueError(
                    "optuna_trials > 0 but optuna_search_space is None; "
                    "either pass a search space or set optuna_trials=0."
                )
            best_params = optimize_params(
                strategy_cls,
                data=train_df,
                base_params=base_params,
                # ``optimize_params`` types ``search_space`` as
                # ``dict[str, tuple]``; we accept ``Mapping`` here so
                # callers can pass any Mapping impl.
                search_space=cast(dict[str, tuple[float, float]], dict(optuna_search_space)),
                n_trials=optuna_trials,
                metric=metric,
                run_backtest_kwargs=base_kwargs,
                backtest_runner=runner,
            )
        else:
            best_params = dict(base_params)

        # 2. Train backtest (IS).
        # W5.1-C4: pass strategy params as top-level kwargs (NOT
        # ``strategy_kwargs=best_params``) because AKQuant's engine
        # only unwraps ``strategy_params=...`` and top-level kwargs.
        # Using top-level kwargs here is the simplest path that
        # ``_split_strategy_kwargs`` routes to ``self.params``.
        train_result = runner(
            data=train_df,
            strategy=strategy_cls,
            **base_kwargs,
            **best_params,
        )
        train_metrics = summarize_metrics(train_result, phase="is")

        # 3. Test backtest (OOS) — same params as IS so the OOS
        # metric reflects the strategy the IS pass actually chose.
        test_result = runner(
            data=test_df,
            strategy=strategy_cls,
            **base_kwargs,
            **best_params,
        )
        test_metrics = summarize_metrics(test_result, phase="oos")

        folds.append(
            FoldResult(
                fold_index=fold_idx,
                train_start=pd.Timestamp(pd.to_datetime(train_df["date"]).min()),
                train_end=pd.Timestamp(pd.to_datetime(train_df["date"]).max()),
                test_start=pd.Timestamp(pd.to_datetime(test_df["date"]).min()),
                test_end=pd.Timestamp(pd.to_datetime(test_df["date"]).max()),
                train_metrics=train_metrics,
                test_metrics=test_metrics,
                best_params=best_params,
            )
        )

    # Aggregate IS→OOS decay from the LAST fold. W5 leaves multi-fold
    # decay (weighted by fold length / equity) to the caller.
    is_to_oos_decay: dict[str, float] = {}
    if folds:
        last = folds[-1]
        # ``summarize_metrics`` returns ``dict[str, float | str]`` (the
        # ``"phase"`` key is a str). oos_decay expects
        # ``dict[str, float]``; cast the numeric entries inline.
        is_metrics = {
            k: float(v) for k, v in last.train_metrics.items() if isinstance(v, (int, float))
        }
        oos_metrics = {
            k: float(v) for k, v in last.test_metrics.items() if isinstance(v, (int, float))
        }
        is_to_oos_decay = oos_decay(is_metrics, oos_metrics)

    return WalkForwardResult(
        folds=folds,
        base_params=base_params,
        metric=metric,
        is_to_oos_decay=is_to_oos_decay,
    )
