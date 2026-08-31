"""Unit tests for ``execution.brokers.xtquant_callbacks`` (W7.1 Phase 2).

The callback subclass marshals events from xtquant's SDK thread
to the runner thread via a ``queue.Queue``. These tests pin:

  * on_order / on_trade / on_order_error / on_disconnected push
    the correct event shape.
  * Status int → ExecutionStatus mapping (``_xt_status_to_execution_status``).
  * Queue-full drops (with drop_count increment + on_drop hook).
  * client_order_id parsing from ``order_remark``.
"""

from __future__ import annotations

import queue
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from execution.brokers.xtquant_callbacks import (  # noqa: E402
    DisconnectedEvent,
    OrderErrorEvent,
    OrderEvent,
    TradeEvent,
    XtQuantTradeCallback,
    _xt_status_to_execution_status,
)
from execution.brokers.xtquant_models import (  # noqa: E402
    XT_ORDER_STATUS_FILLED,
    XtOrder,
    XtTrade,
)


def test_on_order_with_valid_remark_parses_client_id() -> None:
    q: queue.Queue = queue.Queue()
    cb = XtQuantTradeCallback(q)
    cb.on_order(
        XtOrder(
            order_id=42,
            order_remark="q:cid-xyz",
            stock_code="000001.SZ",
            order_type=23,
            price_type=11,
            order_volume=100,
            price=10.0,
            order_status=XT_ORDER_STATUS_FILLED,
        )
    )
    ev = q.get_nowait()
    assert isinstance(ev, OrderEvent)
    assert ev.client_order_id == "cid-xyz"
    assert ev.broker_order_id == 42
    assert ev.status == "filled"
    assert ev.raw_status == XT_ORDER_STATUS_FILLED


def test_on_order_with_unparseable_remark_returns_none_client_id() -> None:
    """Remark without ``q:`` prefix → ``client_order_id=None`` (drop later)."""
    q: queue.Queue = queue.Queue()
    cb = XtQuantTradeCallback(q)
    cb.on_order(
        XtOrder(
            order_id=1,
            order_remark="not_a_remark",
            stock_code="000001.SZ",
            order_type=23,
            price_type=11,
            order_volume=100,
            price=10.0,
        )
    )
    ev = q.get_nowait()
    assert ev.client_order_id is None
    assert ev.broker_order_id == 1


def test_on_trade_pushes_trade_event() -> None:
    q: queue.Queue = queue.Queue()
    cb = XtQuantTradeCallback(q)
    cb.on_trade(
        XtTrade(
            order_id=1,
            stock_code="000001.SZ",
            direction=23,
            traded_volume=100,
            traded_price=10.5,
            traded_amount=1050.0,
            traded_time="2026-09-02T09:30:00",
        )
    )
    ev = q.get_nowait()
    assert isinstance(ev, TradeEvent)
    assert ev.broker_order_id == 1
    assert ev.volume == 100
    assert ev.price == 10.5
    assert ev.amount == 1050.0
    assert ev.timestamp == "2026-09-02T09:30:00"


def test_on_disconnected_pushes_event() -> None:
    q: queue.Queue = queue.Queue()
    cb = XtQuantTradeCallback(q)
    cb.on_disconnected()
    ev = q.get_nowait()
    assert isinstance(ev, DisconnectedEvent)


def test_on_order_error_pushes_event() -> None:
    q: queue.Queue = queue.Queue()
    cb = XtQuantTradeCallback(q)

    class FakeErr:
        error_id = "E123"
        error_msg = "insufficient funds"
        order_id = 7

    cb.on_order_error(FakeErr())
    ev = q.get_nowait()
    assert isinstance(ev, OrderErrorEvent)
    assert ev.error_id == "E123"
    assert ev.error_msg == "insufficient funds"
    assert ev.broker_order_id == 7


def test_queue_full_drops_event() -> None:
    """If the queue is full, ``_enqueue`` drops and increments drop_count."""
    q: queue.Queue = queue.Queue(maxsize=2)
    cb = XtQuantTradeCallback(q)
    cb.on_disconnected()
    cb.on_disconnected()
    cb.on_disconnected()
    cb.on_disconnected()
    assert cb.drop_count == 2  # the last two dropped


def test_status_mapping_all_codes() -> None:
    """All xtquant status ints map to a defined ExecutionStatus."""
    assert _xt_status_to_execution_status(0) == "submitted"
    assert _xt_status_to_execution_status(1) == "submitted"
    assert _xt_status_to_execution_status(2) == "submitted"
    assert _xt_status_to_execution_status(3) == "partial"
    assert _xt_status_to_execution_status(53) == "filled"
    assert _xt_status_to_execution_status(54) == "rejected"
    assert _xt_status_to_execution_status(55) == "cancelled"
    assert _xt_status_to_execution_status(-1) == "submitted"  # unknown → defensive
