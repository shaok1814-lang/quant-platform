"""Cross-section post-processors.

Implements the three post-processing primitives a research pipeline
needs to make raw factor values comparable cross-sectionally:

  * :func:`winsorize`   — 去极值 (3-sigma / MAD / quantile).
  * :func:`standardize` — z-score 标准化.
  * :class:`Neutralizer` + :class:`PassThroughNeutralizer` —
    中性化 hook (industry neutralization data lands in W5).

Boundary semantics are uniform across all three:

  * Empty input → returned unchanged (no raise).
  * All-NaN input → returned unchanged (no raise).
  * Single non-NaN value → returned unchanged (statistics undefined).
  * All-equal input → returned unchanged (std / MAD = 0).
  * NaN positions are preserved; clipping / z-scoring never pushes
    NaNs into the finite-value range.

The library does NOT silently drop rows. Strategy / pipeline layers
that want to drop NaN rows must do so explicitly via ``.dropna()``.
"""

from __future__ import annotations

from typing import Final, Literal, Protocol

import pandas as pd

from research.factor_lib._types import FactorSeries

# Public type alias so callers can spell out the method in
# type-checked signatures without falling back to ``str``.
WinsorMethod = Literal["3sigma", "mad", "quantile"]

# Gaussian-consistency constant for MAD-based clipping. For a
# standard Gaussian, MAD = 1 / 1.4826 * std; multiplying MAD by
# 1.4826 yields a robust std estimate that is comparable to the
# sample std used in the 3-sigma branch.
_MAD_GAUSSIAN_CONSTANT: Final[float] = 1.4826

__all__ = [
    "Neutralizer",
    "PassThroughNeutralizer",
    "WinsorMethod",
    "standardize",
    "winsorize",
]


def winsorize(
    s: pd.Series,
    *,
    method: WinsorMethod = "3sigma",
    sigma: float = 3.0,
    mad_k: float = 3.5,
    lower_q: float = 0.01,
    upper_q: float = 0.99,
) -> FactorSeries:
    """Clip extreme values to reduce single-bar outlier influence.

    Three clipping methods:

      * ``"3sigma"`` — clip to ``[mean - sigma * std, mean + sigma * std]``
        with ``ddof=1`` (sample std). Default ``sigma=3`` is the
        classical "3-sigma rule".
      * ``"mad"`` — clip to ``[median - mad_k * 1.4826 * MAD,
        median + mad_k * 1.4826 * MAD]``. Robust to outliers in
        the clip-target computation itself (the median / MAD use
        ranks). Default ``mad_k=3.5`` matches the Iglewicz-Hoaglin
        convention for outlier labelling. Falls back to
        ``"3sigma"`` if MAD == 0 (constant series).
      * ``"quantile"`` — clip to the
        ``[Series.quantile(lower_q), Series.quantile(upper_q)]``
        interval. Default ``lower_q=0.01, upper_q=0.99`` (1% / 99%).

    All three methods preserve NaN positions; clip does not push
    NaNs into the finite range.

    Args:
        s: Input ``pd.Series`` (typically a factor output).
        method: Clipping strategy. Must be one of ``"3sigma"`` /
            ``"mad"`` / ``"quantile"``.
        sigma: ``"3sigma"``-only. Multiplier on std.
        mad_k: ``"mad"``-only. Multiplier on
            ``1.4826 * MAD``.
        lower_q: ``"quantile"``-only. Lower quantile.
        upper_q: ``"quantile"``-only. Upper quantile.

    Returns:
        ``pd.Series`` with extreme values clipped to the bound.
        Name + index are preserved. Returns the input unchanged
        on empty / all-NaN / all-equal / single-value inputs.

    Raises:
        ValueError: if ``method`` is unknown.
    """
    if s.empty or s.isna().all():
        return s.copy()
    if method == "3sigma":
        mean = s.mean()
        std = s.std(ddof=1)
        if std == 0 or pd.isna(std):
            return s.copy()
        lower, upper = mean - sigma * std, mean + sigma * std
        return s.clip(lower=lower, upper=upper)
    if method == "mad":
        median = s.median()
        mad = (s - median).abs().median()
        if mad == 0 or pd.isna(mad):
            return winsorize(s, method="3sigma", sigma=sigma)
        scaled = mad_k * _MAD_GAUSSIAN_CONSTANT * mad
        return s.clip(lower=median - scaled, upper=median + scaled)
    if method == "quantile":
        lower = s.quantile(lower_q)
        upper = s.quantile(upper_q)
        if pd.isna(lower) or pd.isna(upper):
            return s.copy()
        return s.clip(lower=lower, upper=upper)
    raise ValueError(
        f"Unknown winsorize method: {method!r}. "
        f"Expected one of '3sigma', 'mad', 'quantile'."
    )


def standardize(s: pd.Series, *, ddof: int = 0) -> FactorSeries:
    """Z-score standardize a factor column.

    Formula: ``(s - mean) / std`` with the requested ``ddof``.
    NaN positions are preserved.

    Args:
        s: Input ``pd.Series``.
        ddof: Delta degrees of freedom for ``std``. Default ``0``
            matches the Bollinger-z convention in
            :func:`research.factor_lib.mean_reversion.bollinger_z`
            so cross-section ranks stay comparable.

    Returns:
        ``pd.Series`` with mean ~ 0 and std ~ 1. Returns the input
        unchanged on empty / all-NaN / single-value / all-equal
        inputs (where std is undefined or 0).
    """
    if s.empty or s.isna().all():
        return s.copy()
    mean = s.mean()
    std = s.std(ddof=ddof)
    if std == 0 or pd.isna(std):
        return s.copy()
    return (s - mean) / std


class Neutralizer(Protocol):
    """Pluggable cross-section neutralization seam.

    W3 ships only :class:`PassThroughNeutralizer`; W5 will add an
    ``IndustryNeutralizer`` once the industry mapping data lands.
    The Protocol lets future implementations drop in without
    touching factor or strategy code.

    Implementations must be PURE — no mutation of the input
    ``df_wide``. Return a copy with ``factor_col`` adjusted
    cross-sectionally per ``group_col``.
    """

    def __call__(
        self,
        df_wide: pd.DataFrame,
        factor_col: str,
        group_col: str,
    ) -> pd.DataFrame: ...


class PassThroughNeutralizer:
    """Identity neutralizer — returns the input DataFrame unchanged.

    Lets :class:`~research.factor_lib.pipeline.FactorPipeline` always
    carry a non-None neutralizer (so the dataclass stays
    well-typed) while real neutralization is deferred to W5.
    """

    def __call__(
        self,
        df_wide: pd.DataFrame,
        factor_col: str,
        group_col: str,
    ) -> pd.DataFrame:
        # Return a copy so caller-side mutations do not leak back
        # into the pipeline's working frame.
        return df_wide.copy()
