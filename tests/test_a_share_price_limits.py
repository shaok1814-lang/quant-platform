"""Tests for ``backtest/a_share/price_limits.py`` (W4-C2)."""

from __future__ import annotations

import pytest
from backtest.a_share.price_limits import (
    LIMIT_PCT_BY_BOARD,
    ST_LIMIT_PCT,
    compute_limit_price,
    is_at_limit,
    is_limit_down,
    is_limit_up,
)

# ===========================================================================
# Group 1: per-board upper limit
# ===========================================================================


@pytest.mark.parametrize(
    ("board", "pct"),
    [
        pytest.param("main", 0.10, id="main-10pct"),
        pytest.param("chinext", 0.20, id="chinext-20pct"),
        pytest.param("star", 0.20, id="star-20pct"),
        pytest.param("bjs", 0.30, id="bjs-30pct"),
    ],
)
def test_compute_limit_price_upper_per_board(board: str, pct: float) -> None:
    """``upper_limit == round(prev_close * (1 + pct), 2)`` for each board."""
    bounds = compute_limit_price(10.00, is_st=False, board=board)  # type: ignore[arg-type]
    assert bounds.upper_limit == pytest.approx(round(10.00 * (1 + pct), 2))
    assert bounds.lower_limit == pytest.approx(round(10.00 * (1 - pct), 2))


def test_compute_limit_price_st_overrides_board() -> None:
    """ST always uses the 5% band regardless of board."""
    for board in ("main", "chinext", "star", "bjs"):
        bounds = compute_limit_price(10.00, is_st=True, board=board)  # type: ignore[arg-type]
        assert bounds.upper_limit == pytest.approx(10.50)
        assert bounds.lower_limit == pytest.approx(9.50)


def test_compute_limit_price_exact_boundary_main() -> None:
    """Main board, prev_close=10.00 → upper=11.00, lower=9.00 (hand-computed)."""
    bounds = compute_limit_price(10.00, is_st=False, board="main")
    assert bounds.upper_limit == 11.00
    assert bounds.lower_limit == 9.00


def test_compute_limit_price_rounds_to_two_places() -> None:
    """Rounding precision: prev_close=10.123, upper=round(10.123*1.10, 2).

    Python's banker's rounding on 11.1353 produces 11.14 (the .1353
    is rounded up to .14 rather than down to .13 — the nearest
    representable). The test pins the exact rounded value so the
    rounding behavior is part of the contract.
    """
    bounds = compute_limit_price(10.123, is_st=False, board="main")
    assert bounds.upper_limit == 11.14
    assert bounds.lower_limit == 9.11


# ===========================================================================
# Group 2: is_limit_up / is_limit_down / is_at_limit
# ===========================================================================


def test_is_limit_up_exact_boundary() -> None:
    """Close exactly on the upper limit → True."""
    assert is_limit_up(11.00, 10.00, is_st=False, board="main") is True


def test_is_limit_up_above_boundary_but_unrounded_is_false() -> None:
    """Close above the limit but not on the 0.01 boundary → False."""
    # upper=11.00; close=11.005 (1 tick above) → False.
    assert is_limit_up(11.005, 10.00, is_st=False, board="main") is False


def test_is_limit_up_below_boundary_is_false() -> None:
    """Close below the limit → False."""
    assert is_limit_up(10.50, 10.00, is_st=False, board="main") is False


def test_is_limit_down_exact_boundary() -> None:
    assert is_limit_down(9.00, 10.00, is_st=False, board="main") is True


def test_is_limit_down_just_below_is_false() -> None:
    # lower=9.00; close=8.995 (1 tick below) → False.
    assert is_limit_down(8.995, 10.00, is_st=False, board="main") is False


def test_is_at_limit_symmetric() -> None:
    """``is_at_limit`` is True iff either limit matches."""
    assert is_at_limit(11.00, 10.00, is_st=False, board="main") is True
    assert is_at_limit(9.00, 10.00, is_st=False, board="main") is True
    assert is_at_limit(10.00, 10.00, is_st=False, board="main") is False


def test_is_limit_up_ignores_volume() -> None:
    """Pure price check — volume is irrelevant for the limit-up flag."""
    # The function signature does not even accept volume; this test
    # exists to lock the interface (a refactor that pulled volume
    # in would have to break this test).
    assert is_limit_up(11.00, 10.00, is_st=False, board="main") is True


def test_is_limit_up_with_st_uses_st_band() -> None:
    """ST main, prev_close=10.00 → upper=10.50, lower=9.50."""
    assert is_limit_up(10.50, 10.00, is_st=True, board="main") is True
    assert is_limit_up(11.00, 10.00, is_st=True, board="main") is False  # main board pct, not ST


# ===========================================================================
# Group 3: validation
# ===========================================================================


def test_compute_limit_price_rejects_non_positive_prev_close() -> None:
    with pytest.raises(ValueError, match="prev_close"):
        compute_limit_price(0.0, is_st=False, board="main")
    with pytest.raises(ValueError, match="prev_close"):
        compute_limit_price(-1.0, is_st=False, board="main")


def test_compute_limit_price_rejects_unknown_board() -> None:
    with pytest.raises(ValueError, match="Unknown board"):
        compute_limit_price(10.00, is_st=False, board="nonsense")  # type: ignore[arg-type]


# ===========================================================================
# Group 4: table constants
# ===========================================================================


def test_limit_pct_table_matches_board_spec() -> None:
    """LOCKED spec — per CLAUDE.md A-share boundary list."""
    assert LIMIT_PCT_BY_BOARD["main"] == 0.10
    assert LIMIT_PCT_BY_BOARD["chinext"] == 0.20
    assert LIMIT_PCT_BY_BOARD["star"] == 0.20
    assert LIMIT_PCT_BY_BOARD["bjs"] == 0.30


def test_st_limit_pct_is_five_percent() -> None:
    """LOCKED spec — ST / *ST always ±5%."""
    assert ST_LIMIT_PCT == 0.05
