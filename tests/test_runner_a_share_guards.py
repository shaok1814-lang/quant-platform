"""Tests for the runner-level A-share guards (W7.1 follow-up).

Covers:
  * :func:`execution.risk.check_price_limit` — boundary cases
    for the 4 boards (main 10%, chinext/star 20%, bjs 30%) plus
    ST override (5% regardless of board).
  * :func:`execution.risk.check_suspension` — volume == 0 case.
  * :func:`backtest.a_share.board_lookup.board_for_symbol` —
    prefix mapping for the 4 boards.
  * :func:`execution.risk.check_price_limit` opt-out via
    ``RiskConfig.enable_price_limit_guard = False``.
"""
from __future__ import annotations

import pytest

from backtest.a_share.board_lookup import board_for_symbol
from execution.protocol import RiskConfig
from execution.risk import (
    REASON_PRICE_LIMIT_DOWN,
    REASON_PRICE_LIMIT_UP,
    REASON_SUSPENDED,
    Allow,
    Reject,
    check_price_limit,
    check_suspension,
)
from execution.protocol import OrderIntent

# --- board_for_symbol ---


def test_board_for_symbol_main() -> None:
    assert board_for_symbol("000001") == "main"  # Shenzhen main
    assert board_for_symbol("600519") == "main"  # Shanghai main
    assert board_for_symbol("603000") == "main"
    assert board_for_symbol("002415") == "main"  # SMEB


def test_board_for_symbol_chinext() -> None:
    assert board_for_symbol("300750") == "chinext"
    assert board_for_symbol("301308") == "chinext"


def test_board_for_symbol_star() -> None:
    assert board_for_symbol("688981") == "star"
    assert board_for_symbol("688287") == "star"


def test_board_for_symbol_bjs() -> None:
    assert board_for_symbol("830799") == "bjs"
    assert board_for_symbol("400138") == "bjs"


def test_board_for_symbol_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        board_for_symbol("12345")  # too short
    with pytest.raises(ValueError):
        board_for_symbol("ABCDEF")  # not digits
    with pytest.raises(ValueError):
        board_for_symbol("1234567")  # too long
    with pytest.raises(ValueError):
        board_for_symbol("999999")  # no prefix match


# --- check_price_limit ---


def _intent(side: str, symbol: str = "000001") -> OrderIntent:
    return OrderIntent(
        client_order_id=f"test-{side}-{symbol}",
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        quantity=100,
        price=11.0,
        order_type="limit",
        reason="test",
    )


def test_price_limit_allows_normal_buy() -> None:
    """On a normal bar (not at limit), buys are allowed."""
    cfg = RiskConfig(enable_price_limit_guard=True)
    intent = _intent("buy")
    # prev_close=10.0, current=10.5 (5% up, well below 10% main limit)
    decision = check_price_limit(
        intent, current_close=10.5, prev_close=10.0, board="main", is_st=False, cfg=cfg
    )
    assert isinstance(decision, Allow)


def test_price_limit_blocks_buy_at_main_limit_up() -> None:
    """Main board 10% limit: prev_close=10, close=11.00 → blocked."""
    cfg = RiskConfig(enable_price_limit_guard=True)
    intent = _intent("buy")
    decision = check_price_limit(
        intent, current_close=11.0, prev_close=10.0, board="main", is_st=False, cfg=cfg
    )
    assert isinstance(decision, Reject)
    assert decision.reason.startswith(REASON_PRICE_LIMIT_UP)


def test_price_limit_blocks_buy_at_chinext_limit_up() -> None:
    """ChiNext 20% limit: prev_close=10, close=12.00 → blocked."""
    cfg = RiskConfig(enable_price_limit_guard=True)
    intent = _intent("buy", symbol="300750")
    decision = check_price_limit(
        intent, current_close=12.0, prev_close=10.0, board="chinext", is_st=False, cfg=cfg
    )
    assert isinstance(decision, Reject)
    assert decision.reason.startswith(REASON_PRICE_LIMIT_UP)


def test_price_limit_blocks_sell_at_main_limit_down() -> None:
    """Main board 10% lower: prev_close=10, close=9.00 → blocked."""
    cfg = RiskConfig(enable_price_limit_guard=True)
    intent = _intent("sell")
    decision = check_price_limit(
        intent, current_close=9.0, prev_close=10.0, board="main", is_st=False, cfg=cfg
    )
    assert isinstance(decision, Reject)
    assert decision.reason.startswith(REASON_PRICE_LIMIT_DOWN)


def test_price_limit_st_uses_5pct_band() -> None:
    """ST symbols always use 5% regardless of board (CLAUDE.md)."""
    cfg = RiskConfig(enable_price_limit_guard=True)
    intent = _intent("buy", symbol="000010")  # ST code from snapshot
    # Main board would allow 10% up but ST caps at 5%.
    decision = check_price_limit(
        intent, current_close=10.5, prev_close=10.0, board="main", is_st=True, cfg=cfg
    )
    assert isinstance(decision, Reject)
    assert decision.reason.startswith(REASON_PRICE_LIMIT_UP)


def test_price_limit_disabled_allows_everything() -> None:
    """Guard off: even a 涨停 buy passes."""
    cfg = RiskConfig(enable_price_limit_guard=False)
    intent = _intent("buy")
    decision = check_price_limit(
        intent, current_close=11.0, prev_close=10.0, board="main", is_st=False, cfg=cfg
    )
    assert isinstance(decision, Allow)


def test_price_limit_invalid_prev_close_falls_through() -> None:
    """prev_close <= 0 is defensive Allow (no block)."""
    cfg = RiskConfig(enable_price_limit_guard=True)
    intent = _intent("buy")
    decision = check_price_limit(
        intent, current_close=11.0, prev_close=0.0, board="main", is_st=False, cfg=cfg
    )
    assert isinstance(decision, Allow)


# --- check_suspension ---


def test_suspension_blocks_zero_volume() -> None:
    """volume == 0 → all intents rejected (CLAUDE.md 停牌日无成交)."""
    cfg = RiskConfig(enable_suspension_guard=True)
    intent = _intent("buy")
    decision = check_suspension(intent, current_volume=0, cfg=cfg)
    assert isinstance(decision, Reject)
    assert decision.reason.startswith(REASON_SUSPENDED)


def test_suspension_allows_normal_volume() -> None:
    cfg = RiskConfig(enable_suspension_guard=True)
    intent = _intent("buy")
    decision = check_suspension(intent, current_volume=1_000_000, cfg=cfg)
    assert isinstance(decision, Allow)


def test_suspension_disabled_allows_zero_volume() -> None:
    """Guard off: zero volume does not block."""
    cfg = RiskConfig(enable_suspension_guard=False)
    intent = _intent("buy")
    decision = check_suspension(intent, current_volume=0, cfg=cfg)
    assert isinstance(decision, Allow)


# --- RiskConfig defaults ---


def test_risk_config_defaults_enable_guards() -> None:
    """Per CLAUDE.md both guards default to True (defense-in-depth)."""
    cfg = RiskConfig()
    assert cfg.enable_price_limit_guard is True
    assert cfg.enable_suspension_guard is True
