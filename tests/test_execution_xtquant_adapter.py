"""Unit tests for ``execution.brokers.xtquant_live.XtQuantLiveAdapter`` (W7.1 Phase 2).

These run against ``FakeXtQuantTrader`` so no Windows / xtquant
DLL is needed locally. Coverage:

  * Construction with injected ``trader_factory`` /
    ``account_factory``.
  * connect / subscribe / disconnect lifecycle (idempotent).
  * place_order: encoding ``order_remark`` with ``q:<client_id>``,
    idempotent re-submit, broker-to-client map maintenance.
  * consume_events: drains OrderEvent / TradeEvent / DisconnectedEvent
    into adapter state; ``client_order_id`` resolved for TradeEvents.
  * Reconnect on DisconnectedEvent with exponential backoff.
  * Watchdog: silent TCP half-open triggers force-reconnect.
  * Drop counter surfaces via ``drop_count`` property.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from execution.brokers.xtquant_callbacks import (  # noqa: E402
    DisconnectedEvent,
    OrderEvent,
    TradeEvent,
)
from execution.brokers.xtquant_fake import FakeXtQuantTrader  # noqa: E402
from execution.brokers.xtquant_live import XtQuantLiveAdapter  # noqa: E402
from execution.protocol import OrderIntent  # noqa: E402


def _build_adapter(**overrides: object) -> tuple[XtQuantLiveAdapter, FakeXtQuantTrader]:
    """Factory: build adapter + fake trader with default test setup.

    Returns ``(adapter, trader)`` so tests can drive the trader
    via ``emit_*`` methods.
    """
    trader = FakeXtQuantTrader(path="C:/fake", session_id=42)
    kwargs: dict[str, object] = dict(
        path="C:/fake",
        session_id=42,
        account_id="test-acct",
        trader_factory=lambda: trader,
        account_factory=lambda a, t: ("fake-acct", a, t),
        reconnect_backoff_base_s=0.01,  # fast tests
        watchdog_seconds=0.1,
    )
    kwargs.update(overrides)
    return XtQuantLiveAdapter(**kwargs), trader  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_connect_subscribes_to_account() -> None:
    adapter, trader = _build_adapter()
    assert adapter.is_connected is False
    adapter.connect()
    assert adapter.is_connected
    assert trader._connected
    assert trader._subscribed
    assert trader.connect_count == 1
    assert trader.subscribe_count == 1


def test_connect_idempotent() -> None:
    """Second ``connect()`` is a no-op (counter does NOT advance)."""
    adapter, trader = _build_adapter()
    adapter.connect()
    adapter.connect()
    assert trader.connect_count == 1


def test_disconnect_idempotent() -> None:
    adapter, _ = _build_adapter()
    adapter.disconnect()  # never connected → no-op
    adapter.connect()
    adapter.disconnect()
    adapter.disconnect()  # second time no-op


def test_no_connect_place_order_rejected() -> None:
    """Pre-connect place_order returns rejected report (not raise)."""
    adapter, _ = _build_adapter()
    rep = adapter.place_order(
        OrderIntent(client_order_id="c1", symbol="000001", side="buy", quantity=100, price=10.0),
    )
    assert rep.status == "rejected"
    assert "not connected" in (rep.reject_reason or "")


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------


def test_place_order_encodes_remark_with_client_id_prefix() -> None:
    adapter, trader = _build_adapter()
    adapter.connect()
    rep = adapter.place_order(
        OrderIntent(client_order_id="cid-abc", symbol="000001", side="buy", quantity=100, price=10.0),
    )
    assert rep.status == "submitted"
    assert rep.broker_order_id is not None
    # Verify the remark encoding (the fake returns the order we
    # just placed; its remark should have the q: prefix).
    broker_id = int(rep.broker_order_id)
    order = trader.query_stock_order(None, broker_id)
    assert order is not None
    assert order.order_remark == "q:cid-abc"


def test_place_order_idempotent_same_client_id() -> None:
    """Re-submitting the same intent returns the same broker_order_id."""
    adapter, trader = _build_adapter()
    adapter.connect()
    intent = OrderIntent(client_order_id="dup", symbol="000001", side="buy", quantity=100, price=10.0)
    r1 = adapter.place_order(intent)
    r2 = adapter.place_order(intent)
    assert r1.broker_order_id == r2.broker_order_id
    # Only one order was actually placed.
    assert len(trader.query_stock_orders(None)) == 1


def test_place_order_stores_client_to_broker_map() -> None:
    adapter, _ = _build_adapter()
    adapter.connect()
    adapter.place_order(
        OrderIntent(client_order_id="cid-1", symbol="000001", side="buy", quantity=100, price=10.0),
    )
    assert "cid-1" in adapter.pending_client_ids


# ---------------------------------------------------------------------------
# consume_events
# ---------------------------------------------------------------------------


def test_consume_events_drains_order_and_trade() -> None:
    adapter, trader = _build_adapter()
    adapter.connect()
    rep = adapter.place_order(
        OrderIntent(client_order_id="cid-1", symbol="000001", side="buy", quantity=100, price=10.0),
    )
    # consume the order event from place_order
    events = adapter.consume_events()
    # The fake's order_stock already fired on_order with status SUBMITTED.
    order_events = [e for e in events if isinstance(e, OrderEvent)]
    assert len(order_events) == 1
    assert order_events[0].client_order_id == "cid-1"
    assert order_events[0].status == "submitted"

    # Emit a fill on the fake.
    trader.emit_on_trade(
        order_id=int(rep.broker_order_id),
        stock_code="000001.SZ",
        direction=trader.STOCK_BUY,
        volume=100,
        price=10.5,
    )

    events = adapter.consume_events()
    order_events = [e for e in events if isinstance(e, OrderEvent)]
    trade_events = [e for e in events if isinstance(e, TradeEvent)]
    assert len(order_events) == 1  # status update to FILLED
    assert order_events[0].status == "filled"
    assert len(trade_events) == 1
    # Trade events get client_order_id resolved from broker map.
    assert trade_events[0].client_order_id == "cid-1"


def test_consume_events_handles_disconnected_event() -> None:
    """DisconnectedEvent triggers the reconnect path; after the
    reconnect loop completes the adapter is reconnected."""
    adapter, trader = _build_adapter()
    adapter.connect()
    adapter.place_order(
        OrderIntent(client_order_id="cid-1", symbol="000001", side="buy", quantity=100, price=10.0),
    )
    adapter.consume_events()  # drain initial order event

    # Inject a disconnect via the fake's callback flow.
    trader.emit_on_disconnected()
    events = adapter.consume_events()
    assert any(isinstance(e, DisconnectedEvent) for e in events)
    # The reconnect loop runs synchronously inside consume_events
    # (with backoff_base_s=0.01 in _build_adapter, it completes
    # within milliseconds). Verify it actually retried by
    # checking connect_count went up.
    assert trader.connect_count == 2, (
        "consume_events should have triggered reconnect"
    )
    # After reconnect, orders are accepted again.
    rep2 = adapter.place_order(
        OrderIntent(client_order_id="cid-2", symbol="000001", side="buy", quantity=100, price=10.0),
    )
    assert rep2.status == "submitted"


def test_consume_events_handles_empty_queue() -> None:
    adapter, _ = _build_adapter()
    adapter.connect()
    events = adapter.consume_events()
    assert events == []


# ---------------------------------------------------------------------------
# Watchdog (silent TCP half-open detection)
# ---------------------------------------------------------------------------


def test_watchdog_triggers_reconnect_after_silence() -> None:
    """No events for >watchdog_seconds AND no recent submit → reconnect."""
    adapter, _trader = _build_adapter()
    adapter.connect()
    # Make submit time stale.
    adapter._last_submit_at = time.monotonic() - 100
    adapter._last_event_at = time.monotonic() - 100
    triggered = adapter.watchdog_check()
    assert triggered is True
    # After reconnect (synchronous since backoff is 10ms × 1), adapter should be connected again.
    # Wait briefly for the reconnect loop to complete.
    time.sleep(0.5)
    assert adapter.is_connected


def test_watchdog_no_trigger_during_recent_submit() -> None:
    """Recent submit resets the watchdog window — no false positive."""
    adapter, _ = _build_adapter()
    adapter.connect()
    # Recent submit (within watchdog window).
    adapter._last_submit_at = time.monotonic()
    triggered = adapter.watchdog_check()
    assert triggered is False


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def test_query_account_uses_cache() -> None:
    adapter, trader = _build_adapter()
    adapter.connect()
    trader.set_asset(_make_asset(910_500.0, 900_000.0))
    trader.set_positions([_make_position("000001.SZ", 100, 10.0)])
    adapter.refresh_cache()
    snap = adapter.query_account()
    assert snap.total_equity == pytest.approx(910_500.0)
    assert snap.cash == pytest.approx(900_000.0)


def test_query_positions_uses_cache() -> None:
    adapter, trader = _build_adapter()
    adapter.connect()
    trader.set_positions([_make_position("000001.SZ", 100, 10.0)])
    adapter.refresh_cache()
    positions = adapter.query_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "000001"
    assert positions[0].quantity == 100


def test_query_positions_strips_exchange_suffix() -> None:
    """``"000001.SZ"`` from fake → ``"000001"`` in our Position."""
    adapter, trader = _build_adapter()
    adapter.connect()
    trader.set_positions([_make_position("sh.600000", 200, 15.0)])
    adapter.refresh_cache()
    positions = adapter.query_positions()
    assert positions[0].symbol == "600000"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_asset(total: float, cash: float) -> object:
    from execution.brokers.xtquant_models import XtAsset

    return XtAsset(
        account_id="x", cash=cash, frozen_cash=0.0,
        market_value=total - cash, total_asset=total,
        available_cash=cash,
    )


def _make_position(stock_code: str, volume: int, avg_price: float) -> object:
    from execution.brokers.xtquant_models import XtPosition

    return XtPosition(
        stock_code=stock_code, volume=volume,
        can_use_volume=volume, avg_price=avg_price,
        market_value=volume * avg_price,
    )
