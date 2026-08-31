"""Unit tests for ``execution.risk`` helpers (W7.1).

Each helper is a pure function: a single ``RiskDecision`` returned
from a (state, intent, cfg) tuple. Tests cover boundary behavior
precisely:

  * Position cap:
      - flat → allow (any size)
      - 9.9% + small buy → allow (post < cap)
      - 9.9% + buy that brings post == cap → reject (保守侧)
      - sell → always allow
  * Daily trade count:
      - 0..N-1 → allow; N..N+5 → reject
  * Drawdown kill switch:
      - 0% → allow; 4.99% → allow; 5.00% → reject; 5.01% → reject
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from execution.protocol import (  # noqa: E402
    EquitySnapshot,
    OrderIntent,
    RiskConfig,
    utcnow,
)
from execution.risk import (  # noqa: E402
    Allow,
    Reject,
    check_daily_trade_count,
    check_drawdown_kill_switch,
    check_position_cap,
)

# ---------------------------------------------------------------------------
# Position cap
# ---------------------------------------------------------------------------


def _buy(qty: int = 100, price: float = 10.0) -> OrderIntent:
    return OrderIntent(
        client_order_id="x", symbol="000001", side="buy",
        quantity=qty, price=price,
    )


def _sell(qty: int = 100, price: float = 10.0) -> OrderIntent:
    return OrderIntent(
        client_order_id="y", symbol="000001", side="sell",
        quantity=qty, price=price,
    )


def test_position_cap_flat_allows_any_size() -> None:
    """Flat account + any buy = allow (no existing exposure)."""
    cfg = RiskConfig(max_position_pct=0.10)
    # 9_999 shares * 10 / 1M = 9.999% < 10% cap → allow
    decision = check_position_cap(_buy(qty=9_999), 0, 1_000_000.0, cfg)
    assert isinstance(decision, Allow)


def test_position_cap_just_below_cap_allows() -> None:
    """post-fill = 9.6% (9_500 * 10 + 100 * 10) → allow."""
    cfg = RiskConfig(max_position_pct=0.10)
    decision = check_position_cap(_buy(qty=100), 9_500, 1_000_000.0, cfg)
    assert isinstance(decision, Allow)


def test_position_cap_at_cap_rejects() -> None:
    """post-fill = exactly 10.0% (9_900 + 100 = 10_000 shares) → reject.

    保守侧: a buy that lands the position AT the cap is rejected.
    The next bar the strategy can hold the existing position
    (sells are always allowed).
    """
    cfg = RiskConfig(max_position_pct=0.10)
    decision = check_position_cap(_buy(qty=100), 9_900, 1_000_000.0, cfg)
    assert isinstance(decision, Reject)
    assert decision.reason.startswith("position_cap:")


def test_position_cap_above_cap_rejects() -> None:
    """post-fill = 10.1% (9_900 + 200) → reject with explicit numbers."""
    cfg = RiskConfig(max_position_pct=0.10)
    big = OrderIntent(client_order_id="x", symbol="000001", side="buy", quantity=200, price=10.0)
    decision = check_position_cap(big, 9_900, 1_000_000.0, cfg)
    assert isinstance(decision, Reject)
    assert "10.10%" in decision.reason  # to 2 decimals


def test_position_cap_sell_always_allows() -> None:
    """Sells reduce exposure → never block, even when above cap.

    (The position cap is one-directional: only buys add exposure.)
    """
    cfg = RiskConfig(max_position_pct=0.10)
    # Hypothetically over-cap position; sell should still go through.
    decision = check_position_cap(_sell(qty=99_999), 99_999, 1_000_000.0, cfg)
    assert isinstance(decision, Allow)


def test_position_cap_disabled_allows() -> None:
    """cfg.enabled=False → Allow regardless of size."""
    cfg = RiskConfig(max_position_pct=0.10, enabled=False)
    big = OrderIntent(client_order_id="x", symbol="000001", side="buy", quantity=999_999, price=10.0)
    decision = check_position_cap(big, 0, 1_000_000.0, cfg)
    assert isinstance(decision, Allow)


def test_position_cap_zero_equity_allows() -> None:
    """total_equity <= 0 → no baseline → Allow (defensive)."""
    cfg = RiskConfig(max_position_pct=0.10)
    decision = check_position_cap(_buy(), 0, 0.0, cfg)
    assert isinstance(decision, Allow)


def test_position_cap_missing_price_allows() -> None:
    """Market orders (price=None) → Allow (let the adapter handle)."""
    cfg = RiskConfig(max_position_pct=0.10)
    market = OrderIntent(client_order_id="x", symbol="000001", side="buy", quantity=100, price=None)
    decision = check_position_cap(market, 0, 1_000_000.0, cfg)
    assert isinstance(decision, Allow)


# ---------------------------------------------------------------------------
# Daily trade count cap
# ---------------------------------------------------------------------------


def test_daily_trade_count_below_cap_allows() -> None:
    cfg = RiskConfig(max_daily_trades=20)
    for n in [0, 1, 10, 19]:
        decision = check_daily_trade_count(n, cfg)
        assert isinstance(decision, Allow), f"n={n} should Allow"


def test_daily_trade_count_at_cap_rejects() -> None:
    cfg = RiskConfig(max_daily_trades=20)
    decision = check_daily_trade_count(20, cfg)
    assert isinstance(decision, Reject)
    assert decision.reason.startswith("daily_trade_count:")


def test_daily_trade_count_above_cap_rejects() -> None:
    cfg = RiskConfig(max_daily_trades=20)
    for n in [21, 50, 100]:
        decision = check_daily_trade_count(n, cfg)
        assert isinstance(decision, Reject), f"n={n} should Reject"


def test_daily_trade_count_disabled_allows() -> None:
    cfg = RiskConfig(max_daily_trades=20, enabled=False)
    decision = check_daily_trade_count(100, cfg)
    assert isinstance(decision, Allow)


# ---------------------------------------------------------------------------
# Drawdown kill switch
# ---------------------------------------------------------------------------


def _snap(dd_pct: float) -> EquitySnapshot:
    return EquitySnapshot(
        timestamp=utcnow(),
        cash=950_000.0,
        positions_value=0.0,
        total_equity=950_000.0,
        drawdown_pct=dd_pct,
    )


def test_drawdown_below_cap_allows() -> None:
    cfg = RiskConfig(drawdown_kill_switch_pct=0.05)
    for dd in [0.0, 0.01, 0.04, 0.0499]:
        decision = check_drawdown_kill_switch(_snap(dd), cfg)
        assert isinstance(decision, Allow), f"dd={dd} should Allow"


def test_drawdown_at_cap_rejects() -> None:
    cfg = RiskConfig(drawdown_kill_switch_pct=0.05)
    decision = check_drawdown_kill_switch(_snap(0.05), cfg)
    assert isinstance(decision, Reject)
    assert decision.reason.startswith("drawdown_kill_switch:")


def test_drawdown_above_cap_rejects() -> None:
    cfg = RiskConfig(drawdown_kill_switch_pct=0.05)
    for dd in [0.0501, 0.10, 0.50]:
        decision = check_drawdown_kill_switch(_snap(dd), cfg)
        assert isinstance(decision, Reject), f"dd={dd} should Reject"


def test_drawdown_disabled_allows() -> None:
    cfg = RiskConfig(drawdown_kill_switch_pct=0.05, enabled=False)
    decision = check_drawdown_kill_switch(_snap(0.99), cfg)
    assert isinstance(decision, Allow)
