"""Param sensitivity scan (W5-C3).

CLAUDE.md 防过拟合原则 #3: "参数敏感度: 最优参数附近 ±20% 范围内
表现稳定". This module ships:

  * :func:`param_sensitivity_scan` — sweep one parameter across a
    list of values, return per-value ``{param, metric}`` rows.
  * :func:`assert_stable` — assertion helper: every row's metric is
    within ``±tolerance_pct`` of the base value.

Both work with any AKQuant backtestable strategy. The caller
provides a ``backtest_runner`` (defaults to :func:`akquant.run_backtest`)
so tests can stub it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from akquant.backtest.result import BacktestResult

__all__ = ["assert_stable", "param_sensitivity_scan"]


# Type alias for the backtest runner. Tests inject a stub to avoid
# spinning AKQuant end-to-end. The default is ``akquant.run_backtest``,
# imported lazily inside ``param_sensitivity_scan`` so the unit tests
# do not require AKQuant.
BacktestRunner = Callable[..., "BacktestResult"]


def _default_runner() -> BacktestRunner:
    import akquant  # local import; lazy

    return akquant.run_backtest


def param_sensitivity_scan(
    strategy_cls: type,
    *,
    data: Mapping[str, pd.DataFrame] | pd.DataFrame,
    base_params: dict[str, Any],
    param_name: str,
    param_values: Sequence[Any],
    other_params: dict[str, Any] | None = None,
    metric: str = "sharpe_ratio",
    run_backtest_kwargs: dict[str, Any] | None = None,
    backtest_runner: BacktestRunner | None = None,
) -> pd.DataFrame:
    """Sweep ``param_name`` across ``param_values`` and record ``metric``.

    Per CLAUDE.md "参数敏感度: 最优参数附近 ±20% 范围内表现稳定",
    the typical caller is:

        # Base param is X. Scan ±20% range (e.g. [0.8X, 0.9X, X, 1.1X, 1.2X]).
        df = param_sensitivity_scan(
            strategy_cls, data=data, base_params=base_params,
            param_name="top_n",
            param_values=[4, 5, 6],  # e.g. ±20% around 5
            metric="sharpe_ratio",
        )
        # The base run's metric becomes the center of the stability band.
        base_metric = df.loc[df[param_name] == 5, "sharpe_ratio"].iloc[0]
        assert_stable(df, base_param=5, base_metric_value=base_metric,
                      tolerance_pct=0.20)

    Args:
        strategy_cls: AKQuant ``Strategy`` subclass.
        data: ``Dict[str, pd.DataFrame]`` (multi-symbol) or
            ``pd.DataFrame`` (single). Passed straight to the runner.
        base_params: ``strategy_kwargs`` baseline. Each sweep point
            starts from ``base_params`` and overrides ``param_name``.
        param_name: Parameter to sweep.
        param_values: Values to evaluate at.
        other_params: Additional ``strategy_kwargs`` to merge into
            every sweep point (rarely needed; defaults to ``None``).
        metric: ``BacktestResult.metrics_df`` row to read.
        run_backtest_kwargs: Extra kwargs to forward to ``run_backtest``
            (e.g. ``initial_cash=1_000_000``). Not used for the
            ``backtest_runner`` injection path.
        backtest_runner: Optional injectable runner (tests inject this
            to avoid AKQuant spin-up). Defaults to
            :func:`akquant.run_backtest`.

    Returns:
        ``pd.DataFrame`` with columns ``[param_name, metric]`` and one
        row per ``param_values`` entry.
    """
    if not param_values:
        raise ValueError("param_values must be non-empty")
    runner = backtest_runner if backtest_runner is not None else _default_runner()
    base_kwargs = dict(run_backtest_kwargs or {})
    extra_params = dict(other_params or {})

    out_rows: list[dict[str, Any]] = []
    for v in param_values:
        kwargs = {**base_params, param_name: v, **extra_params}
        result = runner(
            data=data,
            strategy=strategy_cls,
            strategy_kwargs=kwargs,
            **base_kwargs,
        )
        m = float(result.metrics_df.loc[metric, "value"])
        out_rows.append({param_name: v, metric: m})
    return pd.DataFrame(out_rows)


def assert_stable(
    scan_df: pd.DataFrame,
    base_param: Any,
    base_metric_value: float,
    *,
    tolerance_pct: float = 0.20,
    metric: str = "sharpe_ratio",
) -> None:
    """Assert every scan_df[metric] is within ``±tolerance_pct`` of base.

    Per CLAUDE.md "参数敏感度: 最优参数附近 ±20% 范围内表现稳定",
    the canonical tolerance is ``0.20`` (±20%). The band is:

        ``(1 - tolerance_pct) * base_metric_value ≤ v ≤ (1 + tolerance_pct) * base_metric_value``

    For metrics where lower-is-better (max_drawdown, volatility),
    invert the test (caller passes ``tolerance_pct`` accordingly, OR
    inverts the comparison via the base_metric_value sign).

    Raises:
        AssertionError: with a per-row breakdown of which values
            fall outside the band.
        ValueError: if ``metric`` is not a column in ``scan_df``.
    """
    if metric not in scan_df.columns:
        raise ValueError(
            f"scan_df must have a {metric!r} column; columns={list(scan_df.columns)}"
        )
    if not (0.0 <= tolerance_pct <= 1.0):
        raise ValueError(
            f"tolerance_pct must be in [0, 1], got {tolerance_pct}"
        )
    lower = (1.0 - tolerance_pct) * base_metric_value
    upper = (1.0 + tolerance_pct) * base_metric_value
    violations = scan_df[
        (scan_df[metric] < lower) | (scan_df[metric] > upper)
    ]
    if not violations.empty:
        msg_lines = [
            f"CLAUDE.md param-stability invariant violated: "
            f"{metric} outside [{lower:.4f}, {upper:.4f}] (base={base_metric_value:.4f}, "
            f"tolerance_pct={tolerance_pct:.2%})"
        ]
        for _, row in violations.iterrows():
            msg_lines.append(f"  param={row.iloc[0]!r}  metric={row[metric]!r}")
        raise AssertionError("\n".join(msg_lines))
