"""Core validation + thin compute wrapper for the factor library.

The factor library is pandas-only and operates on canonical OHLCV
DataFrames. Every public factor function ultimately consumes the
``CORE_COLUMNS_FACTOR`` columns; ``validate_input_bars`` enforces
this and ``compute_factor`` is a thin wrapper that validates then
delegates to an arbitrary factor function.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

import pandas as pd

from research.factor_lib._types import BarsDF, FactorSeries

# CORE_COLUMNS_FACTOR mirrors
# ``data_layer.ingestion.akshare_fetcher.CORE_COLUMNS`` so the data
# layer's storage schema is a valid factor-library input. ``amount``
# is required for completeness even though current factors do not
# consume it; future volume-weighted factors (VWAP, etc.) will.
#
# IMPORTANT: keep this tuple in lock-step with the data layer's
# CORE_COLUMNS. If the data layer adds/removes a column, update
# here (and bump the factor lib version if the change is breaking).
CORE_COLUMNS_FACTOR: Final[tuple[str, ...]] = (
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)


class MissingColumnError(ValueError):
    """Raised when a DataFrame is missing one or more required columns.

    Subclasses ``ValueError`` so callers that catch ``ValueError``
    generically still see the error.
    """


def validate_input_bars(
    df: BarsDF,
    *,
    require_outstanding: bool = False,
) -> None:
    """Validate that ``df`` carries the canonical OHLCV columns.

    Accepts ``date`` in either a column or the index (DatetimeIndex
    / MultiIndex whose first level is named ``"date"``). The
    MultiIndex case lets pipeline consumers pre-set ``(date,
    symbol)`` without losing the canonical date column.

    Args:
        df: Candidate bars DataFrame.
        require_outstanding: If True, additionally require
            ``outstanding_share`` (an optional data-layer column used
            by ``turnover_ratio``).

    Raises:
        MissingColumnError: with a human-readable message listing ALL
            missing columns at once (not just the first), so callers
            can fix all gaps in one pass.
    """
    required: tuple[str, ...] = CORE_COLUMNS_FACTOR
    if require_outstanding:
        required = (*required, "outstanding_share")
    has_date_in_index = isinstance(df.index, pd.DatetimeIndex) or (
        isinstance(df.index, pd.MultiIndex)
        and len(df.index.names) > 0
        and df.index.names[0] == "date"
    )
    present = set(df.columns)
    if has_date_in_index and "date" not in present:
        # Treat ``date`` as covered by the index so the MultiIndex
        # pipeline path doesn't trigger a false-positive.
        present = {*present, "date"}
    missing = [c for c in required if c not in present]
    if missing:
        suffix = " (+ outstanding_share)" if require_outstanding else ""
        raise MissingColumnError(
            f"DataFrame missing required columns: {missing}. "
            f"Expected at least {CORE_COLUMNS_FACTOR}{suffix}"
        )


def compute_factor(
    df: BarsDF,
    fn: Callable[..., FactorSeries],
    *args: Any,
    **kwargs: Any,
) -> FactorSeries:
    """Validate bars, extract ``df['close']``, apply factor function.

    Suitable for factors that take ``close`` as their first
    positional argument (i.e. most of the trend / momentum /
    mean-reversion family):

        compute_factor(df, ma_deviation, bar_window=20)

    For factors with different input shapes — e.g.
    ``turnover_ratio`` takes ``volume`` + ``outstanding_share`` —
    call them directly without this wrapper.

    Args:
        df: Bars DataFrame. Validated against CORE_COLUMNS_FACTOR
            before the function is applied.
        fn: A factor function whose first argument is a close
            ``pd.Series`` and which returns a ``pd.Series`` aligned
            to ``df.index``.
        *args: Forwarded to ``fn`` after the close Series.
        **kwargs: Forwarded to ``fn``.

    Returns:
        The ``pd.Series`` returned by ``fn``.
    """
    validate_input_bars(df, require_outstanding=False)
    return fn(df["close"], *args, **kwargs)
