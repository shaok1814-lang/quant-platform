"""Tests for ``backtest/a_share/stamp_tax.py`` (W4-C4).

These tests verify the **pure-function contract** (buy → 0, sell →
rate * notional). AKQuant's built-in ``stamp_tax_rate`` parameter
already enforces sell-only at the matcher level; this module is the
strategy-side / offline mirror.
"""

from __future__ import annotations

import pytest
from backtest.a_share.stamp_tax import compute_stamp_tax

# ===========================================================================
# Group 1: buy returns 0
# ===========================================================================


def test_buy_returns_zero_at_default_rate() -> None:
    assert compute_stamp_tax(100_000.0, side="buy") == 0.0


def test_buy_returns_zero_at_custom_rate() -> None:
    assert compute_stamp_tax(100_000.0, side="buy", rate=0.0005) == 0.0


def test_buy_returns_zero_for_zero_notional() -> None:
    """Edge case: zero notional buy → 0 stamp tax."""
    assert compute_stamp_tax(0.0, side="buy") == 0.0


# ===========================================================================
# Group 2: sell returns rate * notional
# ===========================================================================


def test_sell_default_rate_returns_one_tenth_of_one_percent() -> None:
    """Default rate 0.001 → 100 元 stamp on 100k notional."""
    assert compute_stamp_tax(100_000.0, side="sell") == pytest.approx(100.0)


def test_sell_custom_rate() -> None:
    """Custom rate 0.0005 → 50 元 on 100k notional."""
    assert compute_stamp_tax(100_000.0, side="sell", rate=0.0005) == pytest.approx(50.0)


def test_sell_zero_notional_returns_zero() -> None:
    assert compute_stamp_tax(0.0, side="sell") == 0.0


def test_sell_with_high_notional() -> None:
    """Round-trip: 1M notional at 0.001 rate → 1000 stamp tax."""
    assert compute_stamp_tax(1_000_000.0, side="sell") == pytest.approx(1000.0)


# ===========================================================================
# Group 3: validation
# ===========================================================================


def test_negative_notional_raises() -> None:
    with pytest.raises(ValueError, match="notional"):
        compute_stamp_tax(-100.0, side="sell")


def test_negative_rate_raises() -> None:
    with pytest.raises(ValueError, match="rate"):
        compute_stamp_tax(100.0, side="sell", rate=-0.001)


def test_unknown_side_raises() -> None:
    with pytest.raises(ValueError, match="side"):
        compute_stamp_tax(100.0, side="short")  # type: ignore[arg-type]
