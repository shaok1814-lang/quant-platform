"""A-share price-limit (涨跌停) calculations.

The Shanghai / Shenzhen exchanges apply per-day price bands relative
to the previous close:

  * 沪深主板 (``main``): ±10%
  * 创业板 (``chinext``): ±20% (since 2020-08-24 reform)
  * 科创板 (``star``): ±20% (since inaugural 2019-07-22)
  * 北交所 (``bjs``): ±30% (since inaugural 2021-11-15)
  * ST / *ST symbols: ±5% (overrides any board)

Rounding: A-share quotes are 0.01 元 (cents), so the limit price is
``round(prev_close * (1 ± pct), 2)``. This matches the official
limit-price publication rules; daily-ex-div handling at the data
layer is assumed (W2 qfq adjustment).

Boundary semantics:

  * ``is_limit_up`` / ``is_limit_down`` test the EXACT rounded
    limit; values 1 tick above (e.g. 11.005 vs upper bound 11.00)
    are NOT limit-up.
  * Volume is irrelevant — limit-up can occur on a 1-share trade
    at the limit price.
  * ``compute_limit_price`` is the pure function; ``is_*`` are
    convenience predicates built on top.

Note: AKQuant does NOT enforce "no buy on 涨停 / no sell on 跌停"
at the matcher level. This module is the canonical computation;
strategies that need the matcher-level guard must call these
predicates inside ``on_bar`` (see ``backtest/a_share/README.md``).
"""

from __future__ import annotations

from typing import Final

from backtest.a_share._types import (
    DEFAULT_ST_LIMIT_PCT,
    Board,
    LimitBounds,
)

# Per-board limit-pct lookup table. ST / *ST symbols ALWAYS use
# DEFAULT_ST_LIMIT_PCT (5%) regardless of board — see ``compute_limit_price``.
LIMIT_PCT_BY_BOARD: Final[dict[Board, float]] = {
    "main": 0.10,
    "chinext": 0.20,
    "star": 0.20,
    "bjs": 0.30,
}

# Re-export so callers can spell it without importing from ``_types``.
ST_LIMIT_PCT: Final[float] = DEFAULT_ST_LIMIT_PCT

# A-share quote precision: 2 decimal places (0.01 元).
_QUOTE_PRECISION: Final[int] = 2

__all__ = [
    "LIMIT_PCT_BY_BOARD",
    "ST_LIMIT_PCT",
    "compute_limit_price",
    "is_at_limit",
    "is_limit_down",
    "is_limit_up",
]


def _limit_pct(*, is_st: bool, board: Board) -> float:
    """Resolve the effective limit-pct for a (board, ST) tuple.

    ST / *ST symbols always use the tighter 5% band per exchange
    rules, overriding the board-level band.
    """
    if is_st:
        return DEFAULT_ST_LIMIT_PCT
    return LIMIT_PCT_BY_BOARD[board]


def compute_limit_price(
    prev_close: float,
    *,
    is_st: bool,
    board: Board,
) -> LimitBounds:
    """Compute lower / upper limit price for a given (prev_close, is_st, board).

    Args:
        prev_close: Previous trading day's close (qfq-adjusted by the
            data layer; W2 contract).
        is_st: ``True`` if the symbol is flagged as ST / *ST.
        board: Which exchange board the symbol belongs to.

    Returns:
        :class:`LimitBounds` with ``lower_limit`` and ``upper_limit``,
        each rounded to 0.01 元.

    Raises:
        ValueError: if ``prev_close <= 0`` or ``board`` is unknown.
    """
    if prev_close <= 0:
        raise ValueError(f"prev_close must be > 0, got {prev_close}")
    if board not in LIMIT_PCT_BY_BOARD:
        raise ValueError(
            f"Unknown board: {board!r}. Expected one of {list(LIMIT_PCT_BY_BOARD)}"
        )
    pct = _limit_pct(is_st=is_st, board=board)
    upper = round(prev_close * (1.0 + pct), _QUOTE_PRECISION)
    lower = round(prev_close * (1.0 - pct), _QUOTE_PRECISION)
    return LimitBounds(lower_limit=lower, upper_limit=upper)


def is_limit_up(
    close: float,
    prev_close: float,
    *,
    is_st: bool,
    board: Board,
) -> bool:
    """``True`` iff ``close`` sits exactly on the upper limit (rounded)."""
    bounds = compute_limit_price(prev_close, is_st=is_st, board=board)
    return close == bounds.upper_limit


def is_limit_down(
    close: float,
    prev_close: float,
    *,
    is_st: bool,
    board: Board,
) -> bool:
    """``True`` iff ``close`` sits exactly on the lower limit (rounded)."""
    bounds = compute_limit_price(prev_close, is_st=is_st, board=board)
    return close == bounds.lower_limit


def is_at_limit(
    close: float,
    prev_close: float,
    *,
    is_st: bool,
    board: Board,
) -> bool:
    """``True`` iff ``close`` is exactly on either limit (shortcut)."""
    return is_limit_up(
        close, prev_close, is_st=is_st, board=board
    ) or is_limit_down(close, prev_close, is_st=is_st, board=board)
