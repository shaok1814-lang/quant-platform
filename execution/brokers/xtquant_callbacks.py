"""Callback bridge between xtquant's SDK thread and the runner thread.

**Critical invariant**: ``XtQuantTraderCallback`` methods fire on
xtquant's SDK background thread. The runner reads events on the
main thread. Crossing these without marshalling corrupts state
(silent half-written objects, GIL races, etc.).

This module provides the marshalling layer:

  * :class:`BrokerEvent` — frozen dataclasses for the four event
    types (order / trade / order_error / disconnect). Defined as
    plain dataclasses (not the SDK's mutable shapes) so the queue
    contents are immutable + pickle-safe.

  * :class:`XtQuantTradeCallback` — a real
    ``xtquant.xttrader.XtQuantTraderCallback`` subclass that
    pushes events into a ``queue.Queue``. ``register_callback`` on
    the trader wires this up.

  * Drop-on-overflow: if the queue is full (events arriving faster
    than the runner drains), drop and increment a counter. The
    runner reads the counter to surface a hard error in tests /
    dashboards. NEVER block the callback thread (it would
    desync the SDK's event loop).

**Why a queue, not a callback registry**: the runner already
calls ``adapter.consume_events()`` once per bar to drain fills into
the journal. Adding a queue (rather than letting callbacks mutate
adapter state directly) keeps the SDK thread and runner thread
disjoint, which is the only safe pattern in Python with the GIL.
"""

from __future__ import annotations

import queue
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

from execution.protocol import ExecutionStatus

if TYPE_CHECKING:
    from execution.brokers.xtquant_models import XtOrder, XtTrade


__all__ = [
    "BROKER_EVENT_DISCONNECT",
    "BROKER_EVENT_ORDER",
    "BROKER_EVENT_ORDER_ERROR",
    "BROKER_EVENT_TRADE",
    "BrokerEvent",
    "DisconnectedEvent",
    "OrderErrorEvent",
    "OrderEvent",
    "TradeEvent",
    "XtQuantTradeCallback",
]


# Event-type discriminators (string constants for cheap comparison;
# keeping them as module constants lets tests assert exact kind).
BROKER_EVENT_ORDER: str = "order"
BROKER_EVENT_TRADE: str = "trade"
BROKER_EVENT_ORDER_ERROR: str = "order_error"
BROKER_EVENT_DISCONNECT: str = "disconnect"


# Status code → ExecutionStatus mapping (single source of truth).
# Module-level constant so adapter code + tests reference the same
# dict.
def _xt_status_to_execution_status(xt_status: int) -> ExecutionStatus:
    """Map an XtOrder.status int to our :class:`ExecutionStatus` Literal.

    See ``execution/xtquant_models.py`` for the xtquant status
    constant table. The mapping consolidates three xtquant codes
    (0/1/2) into a single ``"submitted"`` because we don't care
    about the granularity inside the broker — all three mean
    "intent accepted, no fill yet".
    """
    from execution.brokers.xtquant_models import (
        XT_ORDER_STATUS_CANCELLED,
        XT_ORDER_STATUS_FILLED,
        XT_ORDER_STATUS_PARTIALLY_FILLED,
        XT_ORDER_STATUS_REJECTED,
    )

    if xt_status in (
        0,
        1,
        2,
    ):
        return "submitted"
    if xt_status == XT_ORDER_STATUS_PARTIALLY_FILLED:
        return "partial"
    if xt_status == XT_ORDER_STATUS_FILLED:
        return "filled"
    if xt_status == XT_ORDER_STATUS_REJECTED:
        return "rejected"
    if xt_status == XT_ORDER_STATUS_CANCELLED:
        return "cancelled"
    # Unknown status (e.g. -1) — treat as submitted defensively.
    # The adapter will see no progress and the runner can decide
    # to escalate (reconcile, alert) on its own.
    return "submitted"


# Expose the mapping function under a public alias too (the
# adapter module imports it as a stable name).
xt_status_to_execution_status = _xt_status_to_execution_status


# ---------------------------------------------------------------------------
# Event dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrokerEvent:
    """Base for all broker events.

    Frozen so concurrent queue readers (runner thread) cannot see
    a half-mutated event. All subclasses add fields below.
    """


@dataclass(frozen=True)
class OrderEvent(BrokerEvent):
    """A status change on an order.

    Fields:
        client_order_id: Our local id (parsed from
            ``XtOrder.order_remark``). ``None`` if the remark was
            empty or not parseable — the adapter drops such events.
        broker_order_id: The xtquant-assigned id.
        status: Our :class:`ExecutionStatus` mapped from xtquant
            status int.
        raw_status: Original xtquant status int (for diagnostics /
            Phase 3 dashboards).
    """

    client_order_id: str | None
    broker_order_id: int
    status: ExecutionStatus
    raw_status: int


@dataclass(frozen=True)
class TradeEvent(BrokerEvent):
    """An execution event (one fill of one order).

    One order can produce multiple TradeEvents (partial fills).
    The runner's ``consume_events`` returns these alongside
    OrderEvents; the journal records each as a separate Fill row.
    """

    client_order_id: str | None
    broker_order_id: int
    stock_code: str
    direction: int
    volume: int
    price: float
    amount: float
    timestamp: str


@dataclass(frozen=True)
class OrderErrorEvent(BrokerEvent):
    """Order-level error (rejection / unknown error).

    Distinct from a "rejected" OrderEvent — this fires when the
    SDK itself reports an error (e.g. async submission failure).
    """

    client_order_id: str | None
    broker_order_id: int | None
    error_id: str
    error_msg: str


@dataclass(frozen=True)
class DisconnectedEvent(BrokerEvent):
    """``on_disconnected`` fired.

    The adapter reacts by setting ``_disconnected = True`` and
    blocking new ``place_order`` calls until reconnect succeeds.
    """


# ---------------------------------------------------------------------------
# Callback subclass
# ---------------------------------------------------------------------------


class XtQuantTradeCallback:
    """Real ``XtQuantTraderCallback`` subclass that enqueues events.

    Methods are simple: each pushes one event into the queue. The
    body is short to minimize time spent on the SDK thread (a slow
    callback can stall the SDK's event loop).

    Drop-on-overflow policy: if the queue is full, the event is
    dropped and a counter incremented. The runner reads the counter
    via :attr:`drop_count` after :meth:`XtQuantLiveAdapter.consume_events`
    to surface hard errors.

    Note: we deliberately do NOT extend the real
    ``XtQuantTraderCallback`` here — xtquant is Windows-only, so
    extending it would crash import on non-Windows. Instead we
    structurally mimic it (same method names / signatures). The
    adapter registers this class with the trader via
    ``trader.register_callback(instance)``; the real SDK accepts
    any object with the right method names (duck-typed).
    """

    # Sentinel prefix for client_order_id in order_remark.
    REMARK_PREFIX: str = "q:"

    def __init__(
        self,
        event_queue: queue.Queue[BrokerEvent],
        *,
        on_drop: callable[[BrokerEvent], None] | None = None,
    ) -> None:
        self._queue = event_queue
        self._on_drop = on_drop
        self.drop_count = 0
        """Number of events dropped due to queue full. Tests
        assert this stays 0 in the happy path."""

    # ---------- SDK callback entry points (duck-typed) ----------

    def on_order(self, order: XtOrder) -> None:
        """Status change on an order."""
        client_id = self._parse_client_id(order.order_remark)
        event = OrderEvent(
            client_order_id=client_id,
            broker_order_id=order.order_id,
            status=_xt_status_to_execution_status(order.order_status),
            raw_status=order.order_status,
        )
        self._enqueue(event)

    def on_trade(self, trade: XtTrade) -> None:
        """One fill event. Multiple may fire for partial fills."""
        # The SDK doesn't echo order_remark on trade events; we
        # rely on the broker_order_id → client_order_id map that
        # the adapter maintains. Put a placeholder here; the
        # adapter's consume_events resolves it before emitting
        # to the runner.
        event = TradeEvent(
            client_order_id=None,
            broker_order_id=trade.order_id,
            stock_code=trade.stock_code,
            direction=trade.direction,
            volume=trade.traded_volume,
            price=trade.traded_price,
            amount=trade.traded_amount,
            timestamp=trade.traded_time,
        )
        self._enqueue(event)

    def on_order_error(self, order_error: object) -> None:
        """Order-level error (e.g. async submit failed).

        ``order_error`` is the SDK's ``XtOrderError`` (no fixed
        shape across versions). We access attribute names defensively.
        """
        error_id = str(getattr(order_error, "error_id", ""))
        error_msg = str(getattr(order_error, "error_msg", ""))
        broker_id = getattr(order_error, "order_id", None)
        client_id = None
        if broker_id is not None:
            # Resolve client_id from broker_id via a callback.
            # We avoid importing the adapter here to keep this
            # module dependency-light; the adapter passes a
            # ``on_drop`` callable when needed and resolves IDs
            # in consume_events using its own map.
            client_id = getattr(order_error, "client_order_id", None)
        event = OrderErrorEvent(
            client_order_id=client_id,
            broker_order_id=broker_id,
            error_id=error_id,
            error_msg=error_msg,
        )
        self._enqueue(event)

    def on_disconnected(self) -> None:
        """TCP drop. Adapter sets ``_disconnected`` and refuses
        new orders until reconnect succeeds."""
        self._enqueue(DisconnectedEvent())

    # ---------- Helpers ----------

    def _enqueue(self, event: BrokerEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self.drop_count += 1
            logger.error(
                "xtquant callback queue full, dropping event kind={k} (drop_count={d})",
                k=type(event).__name__,
                d=self.drop_count,
            )
            if self._on_drop is not None:
                try:
                    self._on_drop(event)
                except Exception:  # pragma: no cover -- drop hook best-effort
                    logger.exception("on_drop callback raised; ignoring")

    @staticmethod
    def _parse_client_id(order_remark: str) -> str | None:
        """Reverse ``f"q:{client_order_id}"`` encoding.

        Returns ``None`` if the remark doesn't start with the
        sentinel prefix (e.g. an order placed by some other tool
        that uses the same broker account). The adapter drops
        such events with a log line.
        """
        if not order_remark:
            return None
        if not order_remark.startswith(XtQuantTradeCallback.REMARK_PREFIX):
            return None
        return order_remark[len(XtQuantTradeCallback.REMARK_PREFIX):]
