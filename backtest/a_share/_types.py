"""Shared types and constants for the W4 A-share rules patch layer."""

from __future__ import annotations

from typing import Final, Literal, NamedTuple

# A-share board identifiers. ``main`` = 沪深主板 (±10%),
# ``chinext`` = 创业板 (±20%), ``star`` = 科创板 (±20%),
# ``bjs`` = 北交所 (±30%). ST / *ST 始终 ±5% (overrides any board).
Board = Literal["main", "chinext", "star", "bjs"]


class LimitBounds(NamedTuple):
    """Lower / upper limit price for a given (prev_close, board, is_st) tuple.

    Returned by :func:`backtest.a_share.price_limits.compute_limit_price`.
    """

    lower_limit: float
    upper_limit: float


# 100-share lot is the A-share floor for non-convertible-bond symbols;
# convertible bonds trade in lots of 10 (out of W4 scope).
DEFAULT_LOT_SIZE: Final[int] = 100

# A-share stamp tax: 0.1% sell-side only (买方不收印花税).
DEFAULT_STAMP_TAX_RATE: Final[float] = 0.001

# Special-treatment (ST / *ST) symbols have a 5% ±5% limit (NOT 10/20/30).
DEFAULT_ST_LIMIT_PCT: Final[float] = 0.05


__all__ = [
    "DEFAULT_LOT_SIZE",
    "DEFAULT_STAMP_TAX_RATE",
    "DEFAULT_ST_LIMIT_PCT",
    "Board",
    "LimitBounds",
]
