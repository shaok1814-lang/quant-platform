"""Tests for ``backtest/a_share/lot_enforcement.py`` (W4-C4)."""

from __future__ import annotations

import pytest
from backtest.a_share.lot_enforcement import enforce_lot, is_valid_lot

# ===========================================================================
# Group 1: round-down behavior
# ===========================================================================


def test_enforce_lot_exact_multiple_unchanged() -> None:
    assert enforce_lot(100) == 100
    assert enforce_lot(200) == 200
    assert enforce_lot(500) == 500


def test_enforce_lot_rounds_down_to_nearest_lot() -> None:
    assert enforce_lot(150) == 100  # 1.5 → 1
    assert enforce_lot(250) == 200  # 2.5 → 2
    assert enforce_lot(999) == 900  # 9.99 → 9


def test_enforce_lot_one_lot_floor() -> None:
    assert enforce_lot(99) == 0  # below 1 lot → 0
    assert enforce_lot(1) == 0  # 1 share with lot=100 → 0 (round-down to floor)


def test_enforce_lot_custom_lot_size() -> None:
    """``lot_size=10`` (e.g. for convertible bonds) is honored."""
    assert enforce_lot(150, lot_size=10) == 150
    assert enforce_lot(155, lot_size=10) == 150


# ===========================================================================
# Group 2: is_valid_lot predicate
# ===========================================================================


def test_is_valid_lot_true_for_multiples() -> None:
    assert is_valid_lot(100) is True
    assert is_valid_lot(300) is True


def test_is_valid_lot_false_for_non_multiples() -> None:
    assert is_valid_lot(150) is False
    assert is_valid_lot(99) is False


def test_is_valid_lot_false_for_zero_or_negative() -> None:
    assert is_valid_lot(0) is False
    assert is_valid_lot(-100) is False


def test_is_valid_lot_false_for_non_positive_lot_size() -> None:
    assert is_valid_lot(100, lot_size=0) is False
    assert is_valid_lot(100, lot_size=-1) is False


# ===========================================================================
# Group 3: validation
# ===========================================================================


def test_enforce_lot_rejects_zero_quantity() -> None:
    with pytest.raises(ValueError, match="quantity"):
        enforce_lot(0)


def test_enforce_lot_rejects_negative_quantity() -> None:
    with pytest.raises(ValueError, match="quantity"):
        enforce_lot(-100)


def test_enforce_lot_rejects_zero_lot_size() -> None:
    with pytest.raises(ValueError, match="lot_size"):
        enforce_lot(100, lot_size=0)


def test_enforce_lot_rejects_negative_lot_size() -> None:
    with pytest.raises(ValueError, match="lot_size"):
        enforce_lot(100, lot_size=-1)
