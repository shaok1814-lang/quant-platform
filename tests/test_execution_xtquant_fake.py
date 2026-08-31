"""Unit tests for ``execution.brokers.xtquant_fake.FakeXtQuantTrader`` (W7.1 Phase 2).

The fake is the test driver for ``XtQuantLiveAdapter`` — these
tests pin its behavior so the adapter's tests can rely on it.

Coverage:
  * Lifecycle: connect / subscribe / disconnect (idempotency).
  * Order placement: returns broker_order_id; idempotent on
    order_remark.
  * fail_next_connect knob: returns rc=1 once, then succeeds.
  * Query methods return XtOrder / XtTrade / XtPosition / XtAsset
    instances (not the real SDK shapes — those don't exist
    without xtquant installed).
  * Test driver methods (``emit_on_*``) update internal state
    and fire callbacks.
  * ``reset()`` clears book + counters for phase-based tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
from execution.brokers.xtquant_fake import FakeXtQuantTrader  # noqa: E402
from execution.brokers.xtquant_models import (  # noqa: E402
    XT_ORDER_STATUS_FILLED,
    XtAsset,
    XtPosition,
)


def test_lifecycle_connect_disconnect_idempotent() -> None:
    t = FakeXtQuantTrader(path="C:/fake", session_id=1)
    assert t._connected is False
    rc = t.connect()
    assert rc == 0 and t._connected
    rc = t.connect()  # still connected → no-op
    assert rc == 0
    assert t.connect_count == 1
    t.disconnect()
    assert t._connected is False
    t.disconnect()  # already disconnected → no-op
    assert t.disconnect_count == 1


def test_subscribe_marks_subscribed() -> None:
    t = FakeXtQuantTrader()
    t.connect()
    assert t._subscribed is False
    rc = t.subscribe(None)
    assert rc == 0 and t._subscribed
    assert t.subscribe_count == 1


def test_fail_next_connect_returns_nonzero_once() -> None:
    t = FakeXtQuantTrader()
    t.connect()  # first connect succeeds
    t.disconnect()
    t.fail_next_connect = True
    rc = t.connect()
    assert rc == 1
    assert t._connected is False
    # Retry succeeds.
    rc = t.connect()
    assert rc == 0 and t._connected


def test_order_stock_returns_unique_id() -> None:
    t = FakeXtQuantTrader()
    t.connect()
    a = t.order_stock(None, "000001.SZ", t.STOCK_BUY, 100, t.FIX_PRICE, 10.0, order_remark="q:a")
    b = t.order_stock(None, "000002.SZ", t.STOCK_BUY, 200, t.FIX_PRICE, 20.0, order_remark="q:b")
    assert a == 1 and b == 2
    assert a != b


def test_order_stock_idempotent_on_remark() -> None:
    """Same remark → same id (no duplicate orders)."""
    t = FakeXtQuantTrader()
    t.connect()
    a = t.order_stock(None, "000001.SZ", t.STOCK_BUY, 100, t.FIX_PRICE, 10.0, order_remark="q:dup")
    b = t.order_stock(None, "000001.SZ", t.STOCK_BUY, 100, t.FIX_PRICE, 10.0, order_remark="q:dup")
    assert a == b


def test_order_stock_empty_remark_each_unique() -> None:
    """Empty remark → no idempotency (each call gets a new id)."""
    t = FakeXtQuantTrader()
    t.connect()
    a = t.order_stock(None, "000001.SZ", t.STOCK_BUY, 100, t.FIX_PRICE, 10.0, order_remark="")
    b = t.order_stock(None, "000001.SZ", t.STOCK_BUY, 100, t.FIX_PRICE, 10.0, order_remark="")
    assert a != b


def test_query_orders_returns_list() -> None:
    t = FakeXtQuantTrader()
    t.connect()
    t.order_stock(None, "000001.SZ", t.STOCK_BUY, 100, t.FIX_PRICE, 10.0, order_remark="q:1")
    t.order_stock(None, "000002.SZ", t.STOCK_BUY, 200, t.FIX_PRICE, 20.0, order_remark="q:2")
    orders = t.query_stock_orders(None)
    assert len(orders) == 2


def test_emit_on_trade_updates_order_and_records_trade() -> None:
    """Fill → order status 53 (FILLED) + trade in query_stock_trades."""
    t = FakeXtQuantTrader()
    t.connect()
    oid = t.order_stock(None, "000001.SZ", t.STOCK_BUY, 100, t.FIX_PRICE, 10.0, order_remark="q:fill")

    fired: list[str] = []

    class Cb:
        def on_order(self, o):
            fired.append(("order", o.order_status))

        def on_trade(self, t):
            fired.append(("trade", t.traded_volume))

    t.register_callback(Cb())
    t.emit_on_trade(
        order_id=oid,
        stock_code="000001.SZ",
        direction=t.STOCK_BUY,
        volume=100,
        price=10.5,
    )

    assert ("order", XT_ORDER_STATUS_FILLED) in fired
    assert ("trade", 100) in fired

    order = t.query_stock_order(None, oid)
    assert order is not None
    assert order.order_status == XT_ORDER_STATUS_FILLED
    assert order.traded_volume == 100
    assert order.traded_price == pytest.approx(10.5)

    trades = t.query_stock_trades(None)
    assert trades is not None and len(trades) == 1
    assert trades[0].traded_amount == pytest.approx(1050.0)


def test_emit_on_disconnected_no_arg() -> None:
    """``emit_on_disconnected()`` fires the on_disconnected callback."""
    t = FakeXtQuantTrader()
    fired: list[str] = []

    class Cb:
        def on_disconnected(self):
            fired.append("disconnect")

    t.register_callback(Cb())
    t.emit_on_disconnected()
    assert fired == ["disconnect"]


def test_set_asset_and_positions_propagate_to_query() -> None:
    """Tests inject asset + positions → query_stock_* returns them."""
    t = FakeXtQuantTrader()
    t.connect()
    asset = XtAsset(
        account_id="x", cash=900_000.0, frozen_cash=0.0,
        market_value=10_500.0, total_asset=910_500.0,
    )
    t.set_asset(asset)
    positions = [
        XtPosition(stock_code="000001.SZ", volume=100, can_use_volume=100, avg_price=10.0),
    ]
    t.set_positions(positions)

    a = t.query_stock_asset(None)
    assert a is not None and a.total_asset == pytest.approx(910_500.0)

    p = t.query_stock_positions(None)
    assert p is not None and len(p) == 1
    assert p[0].volume == 100


def test_reset_clears_book() -> None:
    """``reset()`` clears orders + trades + asset + counters."""
    t = FakeXtQuantTrader()
    t.connect()
    t.order_stock(None, "000001.SZ", t.STOCK_BUY, 100, t.FIX_PRICE, 10.0, order_remark="q:1")
    assert len(t.query_stock_orders(None)) == 1

    t.reset()

    assert t._connected is False
    assert t.connect_count == 0
    assert t.query_stock_orders(None) is None  # not connected → None
    # After reconnect, book is empty.
    t.connect()
    assert len(t.query_stock_orders(None)) == 0


def test_query_returns_none_when_disconnected() -> None:
    """All query methods return ``None`` when not connected."""
    t = FakeXtQuantTrader()
    assert t.query_stock_orders(None) is None
    assert t.query_stock_trades(None) is None
    assert t.query_stock_positions(None) is None
    assert t.query_stock_asset(None) is None
