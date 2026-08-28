"""100-share lot enforcement (CLAUDE.md 最小交易单位).

AKQuant already enforces lot size at order time: ``Instrument.lot_size``
is forwarded to the Rust matcher which rejects non-multi-multi buy
orders (per W4 survey of ``strategy_trading_api.py``). The exception
is ``close_position()``, which **deliberately bypasses** lot rounding
so a position can be fully unwound even if it sits at an odd lot.

This module is the strategy-side / pre-check equivalent of AKQuant's
matcher logic — useful for:

  * Strategies that pre-compute target quantities in Python before
    sending orders (round down before submission to avoid a reject).
  * Tests that want to verify the AKQuant contract locally without
    running a full backtest.

Boundary semantics:

  * ``enforce_lot(0)`` and ``enforce_lot(negative)`` raise
    ``ValueError`` — quantity must be strictly positive.
  * ``enforce_lot`` always **rounds DOWN** to the nearest lot so a
    partial lot never silently inflates into a buy.
"""

from __future__ import annotations

from backtest.a_share._types import DEFAULT_LOT_SIZE

__all__ = ["enforce_lot", "is_valid_lot"]


def _validate(quantity: int, lot_size: int) -> None:
    if quantity <= 0:
        raise ValueError(
            f"quantity must be > 0, got {quantity}"
        )
    if lot_size <= 0:
        raise ValueError(
            f"lot_size must be > 0, got {lot_size}"
        )


def enforce_lot(quantity: int, *, lot_size: int = DEFAULT_LOT_SIZE) -> int:
    """Round ``quantity`` DOWN to the nearest multiple of ``lot_size``.

    Args:
        quantity: Target share count (must be > 0).
        lot_size: Lot size (default 100 for A-share floor).

    Returns:
        Largest integer ``<= quantity`` that is a multiple of
        ``lot_size``.

    Raises:
        ValueError: if ``quantity <= 0`` or ``lot_size <= 0``.
    """
    _validate(quantity, lot_size)
    return (quantity // lot_size) * lot_size


def is_valid_lot(quantity: int, *, lot_size: int = DEFAULT_LOT_SIZE) -> bool:
    """``True`` iff ``quantity`` is a positive multiple of ``lot_size``."""
    if quantity <= 0 or lot_size <= 0:
        return False
    return quantity % lot_size == 0
