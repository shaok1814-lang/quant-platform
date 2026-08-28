"""A-share stamp tax (印花税) — sell-side only.

AKQuant's ``stamp_tax_rate`` parameter is single-sided for sell by
default (per ``StrategyConfig.stamp_tax_rate`` docstring and per the
W4 survey of the matcher). This module is the pure-function
equivalent for offline tests / sanity checks.

Contract (locked):

  * ``compute_stamp_tax(notional, side="buy") == 0.0``
  * ``compute_stamp_tax(notional, side="sell") == rate * notional``

Rate default is ``DEFAULT_STAMP_TAX_RATE = 0.001`` (0.1%, the A-share
sell-side stamp tax rate; northbound stocks via Stock Connect have
a different rate — W4 does not cover that here).
"""

from __future__ import annotations

from typing import Literal

from backtest.a_share._types import DEFAULT_STAMP_TAX_RATE

__all__ = ["Side", "compute_stamp_tax"]

Side = Literal["buy", "sell"]


def compute_stamp_tax(
    notional: float,
    *,
    side: str,
    rate: float = DEFAULT_STAMP_TAX_RATE,
) -> float:
    """Compute the stamp tax for a single fill.

    Args:
        notional: Fill notional (price * quantity). Must be >= 0.
        side: ``"buy"`` returns 0; ``"sell"`` returns ``rate * notional``.
        rate: Stamp tax rate. Default 0.001.

    Returns:
        The stamp tax amount in 元.

    Raises:
        ValueError: if ``notional < 0``, ``rate < 0``, or ``side`` is
            not ``"buy"`` / ``"sell"``.
    """
    if notional < 0:
        raise ValueError(f"notional must be >= 0, got {notional}")
    if rate < 0:
        raise ValueError(f"rate must be >= 0, got {rate}")
    if side == "buy":
        return 0.0
    if side == "sell":
        return float(rate * notional)
    raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
