"""Performance analytics (W5-C2).

Wraps :class:`akquant.backtest.result.BacktestResult` to:

  * Extract a flat dict of key metrics (``summarize_metrics``) with
    an explicit ``phase`` label ("is" / "oos") per CLAUDE.md
    "backtest-result conversations MUST tag every metric as
    in-sample or out-of-sample".
  * Compute OOS / IS ratio per metric (``oos_decay``) so a caller
    can assert the canonical CLAUDE.md "OOS performance decay < 30%"
    invariant: ``sharpe_ratio_ratio >= 0.70`` (higher-is-better metrics).

Why not re-derive Sharpe / Sortino from scratch? ``BacktestResult.metrics_df``
already carries the full Rust-pre-computed set (Sharpe, Sortino,
Calmar, UPI, std_error, VaR, CVaR, volatility, win_rate,
max_drawdown, total_return_pct, exposure_time_pct, etc.). This
module is the thin wrapper that:

  * Pulls the canonical subset the W5 walk-forward + optuna code
    paths actually inspect (avoids accidentally promoting noise
    metrics like ``std_error`` to a required assertion).
  * Tags the dict with a ``phase`` so every consumer (callers,
    tests, loguru) can tell IS vs OOS at a glance.
  * Aggregates per-metric ratios for the CLAUDE.md decay test.

This module does NOT itself re-compute metrics. ``daily_returns``
``/`` ``equity_curve`` re-export the wrapper properties of the
same name on ``BacktestResult`` for symmetry with the rest of the
W5 surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from akquant.backtest.result import BacktestResult

__all__ = [
    "KEY_METRICS",
    "daily_returns",
    "equity_curve",
    "oos_decay",
    "summarize_metrics",
]

# Canonical key metrics the W5 walk-forward + optuna paths inspect.
# Kept narrow on purpose: too many metrics = noise in decay ratios.
# Each entry is the index name in ``BacktestResult.metrics_df``.
KEY_METRICS: tuple[str, ...] = (
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "max_drawdown",
    "total_return_pct",
    "volatility",
    "win_rate",
    "exposure_time_pct",
)

# AKQuant adds these beyond the Rust pre-computed set (see
# result.py:425-495). Include them so callers can sanity-check
# fold activity without a separate trades_df length read.
_AKK_WRAPPER_METRICS: tuple[str, ...] = (
    "closed_trade_count",
    "execution_count",
)


def _coerce_metric_value(value: object) -> float | None:
    """Best-effort coerce a ``metrics_df`` cell to ``float``.

    Returns ``None`` if the cell is NaN / non-numeric so callers
    can skip it (NaN propagates through ratio math otherwise).
    """
    if value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if pd.isna(f):
        return None
    return f


def summarize_metrics(
    result: BacktestResult,
    *,
    phase: str = "is",
) -> dict[str, float | str]:
    """Extract key metrics from ``BacktestResult.metrics_df``.

    Args:
        result: AKQuant ``BacktestResult``.
        phase: Either ``"is"`` (in-sample) or ``"oos"`` (out-of-sample).
            Embedded as the ``phase`` key of the output dict per
            CLAUDE.md "必须明确标注'样本内'还是'样本外'".

    Returns:
        ``dict[str, float]`` with keys:

          * ``"phase"`` — the input ``phase`` arg, verbatim.
          * each metric in :data:`KEY_METRICS` + :data:`_AKK_WRAPPER_METRICS`
            that is present in ``result.metrics_df``.
        Metrics whose cells are NaN / non-numeric are skipped (no key
        in the output).

    Raises:
        ValueError: if ``phase`` is not in ``{"is", "oos"}``.
    """
    if phase not in ("is", "oos"):
        raise ValueError(
            f"phase must be 'is' or 'oos', got {phase!r}. CLAUDE.md "
            "requires explicit IS vs OOS labeling on every metric."
        )
    metrics_df = result.metrics_df
    out: dict[str, float | str] = {"phase": phase}
    for key in (*KEY_METRICS, *_AKK_WRAPPER_METRICS):
        if key not in metrics_df.index:
            continue
        v = _coerce_metric_value(metrics_df.loc[key, "value"])
        if v is not None:
            out[key] = v
    return out


def oos_decay(
    in_sample: dict[str, float],
    out_of_sample: dict[str, float],
) -> dict[str, float]:
    """Compute per-metric ``out_of_sample / in_sample`` ratios.

    Args:
        in_sample: ``summarize_metrics`` output with ``phase="is"``.
        out_of_sample: ``summarize_metrics`` output with ``phase="oos"``.

    Returns:
        ``dict[str, float]`` mapping ``"<metric>_ratio"`` → ratio.
        Metrics that are missing on either side, NaN, or have
        ``in_sample == 0`` are SKIPPED (not surfaced as ratios so
        callers don't have to defensively check each key).

    Caller pattern (CLAUDE.md "测试集表现衰减 < 30%"):

        decay = oos_decay(is_metrics, oos_metrics)
        # Higher-is-better: assert >= 0.70
        assert decay.get("sharpe_ratio_ratio", 0.0) >= 0.70
        # Lower-is-better (drawdown, volatility): assert <= 1.30
        assert decay.get("max_drawdown_ratio", float("inf")) <= 1.30
    """
    out: dict[str, float] = {}
    for key, is_val in in_sample.items():
        if key == "phase":
            continue
        oos_val = out_of_sample.get(key)
        if oos_val is None:
            continue
        if not (pd.notna(is_val) and pd.notna(oos_val)):
            continue
        if is_val == 0:
            # divide-by-zero is undefined — skip.
            continue
        out[f"{key}_ratio"] = float(oos_val) / float(is_val)
    return out


def equity_curve(result: BacktestResult) -> pd.Series:
    """Thin wrapper for ``result.equity_curve``.

    Returned ``pd.Series`` has tz-aware DatetimeIndex (Asia/Shanghai
    by default per AKQuant). Empty if AKQuant's Rust layer
    produced no equity points.
    """
    return result.equity_curve


def daily_returns(result: BacktestResult) -> pd.Series:
    """Thin wrapper for ``result.daily_returns``.

    Returned ``pd.Series`` has tz-aware DatetimeIndex, values are
    float pct changes of the equity curve.
    """
    return result.daily_returns
