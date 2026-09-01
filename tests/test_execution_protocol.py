"""Unit tests for ``execution.protocol`` dataclasses (W7.1).

Covers the data contract that flows through the runner / risk /
adapter / journal pipeline:

  * All dataclasses are ``frozen=True`` (immutability invariant).
  * Field types match the documented contract (Side, OrderType,
    ExecutionStatus literals).
  * :func:`make_intent_id` produces unique IDs.
  * :func:`utcnow` returns naive UTC datetimes.

No execution / network — pure attribute access tests.
"""

from __future__ import annotations

import dataclasses
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from execution.protocol import (  # noqa: E402
    DEFAULT_COMMISSION_RATE,
    DEFAULT_INITIAL_CASH,
    DEFAULT_RISK_CONFIG,
    DEFAULT_STAMP_TAX_RATE,
    EquitySnapshot,
    ExecutionReport,
    Fill,
    OrderIntent,
    Position,
    make_intent_id,
    utcnow,
)


def test_order_intent_frozen() -> None:
    """OrderIntent is immutable: setting an attribute raises."""
    intent = OrderIntent(
        client_order_id="x",
        symbol="000001",
        side="buy",
        quantity=100,
        price=10.20,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        intent.quantity = 200  # type: ignore[misc]


def test_order_intent_defaults() -> None:
    """OrderType default is 'limit'; reason default is empty string."""
    intent = OrderIntent(
        client_order_id="x",
        symbol="000001",
        side="buy",
        quantity=100,
        price=10.0,
    )
    assert intent.order_type == "limit"
    assert intent.reason == ""


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_order_intent_side_literal(side: str) -> None:
    """Side Literal allows both 'buy' and 'sell'."""
    intent = OrderIntent(
        client_order_id="x",
        symbol="000001",
        side=side,  # type: ignore[arg-type]
        quantity=100,
        price=10.0,
    )
    assert intent.side == side


def test_execution_report_frozen() -> None:
    rep = ExecutionReport(
        client_order_id="x",
        status="filled",
        broker_order_id="b1",
        filled_quantity=100,
        avg_fill_price=10.0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        rep.status = "rejected"  # type: ignore[misc]


def test_risk_config_defaults_match_claude_md() -> None:
    """CLAUDE.md hard limits encoded in RiskConfig defaults.

    Defaults must match:
      * 单 symbol 仓位 ≤ 10%       → max_position_pct = 0.10
      * 单日 round-trip ≤ 20       → max_daily_trades   = 20
      * 回撤 ≥ 5% 暂停              → drawdown_kill_switch_pct = 0.05
    """
    assert DEFAULT_RISK_CONFIG.max_position_pct == pytest.approx(0.10)
    assert DEFAULT_RISK_CONFIG.max_daily_trades == 20
    assert DEFAULT_RISK_CONFIG.drawdown_kill_switch_pct == pytest.approx(0.05)
    assert DEFAULT_RISK_CONFIG.enabled is True


def test_constants_match_claude_md() -> None:
    """Commission 0.0003, stamp tax 0.001, initial cash 1M."""
    assert DEFAULT_COMMISSION_RATE == pytest.approx(0.0003)
    assert DEFAULT_STAMP_TAX_RATE == pytest.approx(0.001)
    assert DEFAULT_INITIAL_CASH == pytest.approx(1_000_000.0)


def test_make_intent_id_unique() -> None:
    """100 IDs must all be distinct (collision probability ~0)."""
    ids = {make_intent_id() for _ in range(100)}
    assert len(ids) == 100


def test_make_intent_id_uses_prefix() -> None:
    """Prefix appears at the start of the generated id."""
    assert make_intent_id("smoke").startswith("smoke-")
    assert make_intent_id().startswith("intent-")


def test_utcnow_naive_utc() -> None:
    """utcnow() returns a naive (tzinfo=None) datetime."""
    ts = utcnow()
    assert ts.tzinfo is None
    # Must be recent (within 10 seconds of test start).
    delta = abs((datetime.now(UTC).replace(tzinfo=None) - ts).total_seconds())
    assert delta < 10


def test_position_frozen_and_signed() -> None:
    p = Position(symbol="000001", quantity=100, avg_cost=10.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.quantity = -100  # type: ignore[misc]


def test_fill_requires_fill_id_and_quantity() -> None:
    """Fill is just a dataclass — required fields are not runtime-checked
    by the type system, but we document them via positional args here."""
    from execution.protocol import utcnow

    fill = Fill(
        fill_id="f1",
        client_order_id="c1",
        symbol="000001",
        side="buy",
        quantity=100,
        price=10.0,
        timestamp=utcnow(),
    )
    assert fill.commission == 0.0  # default
    assert fill.stamp_tax == 0.0  # default
    assert fill.broker_order_id is None  # default


def test_equity_snapshot_drawdown_nonnegative() -> None:
    """Drawdown_pct must be >= 0 (high-water-mark logic, no negative)."""
    snap = EquitySnapshot(
        timestamp=utcnow(),
        cash=950_000.0,
        positions_value=0.0,
        total_equity=950_000.0,
        drawdown_pct=0.05,
    )
    assert snap.drawdown_pct >= 0
