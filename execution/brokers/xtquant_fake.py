"""In-process replacement for ``xtquant.xttrader.XtQuantTrader`` (W7.1 Phase 2).

The real xtquant SDK is Windows-only (DLL-bound). Local CI runs
on non-Windows machines and the dev machine here doesn't have
xtquant installed (confirmed via ``importlib.util.find_spec``).
Without a test double, ``XtQuantLiveAdapter`` code paths can
only be exercised on a Windows box with the broker — which kills
the iteration loop.

**This module exists to fix that.** ``FakeXtQuantTrader`` is a
pure-Python stand-in that:

  * Implements the same public-method surface the
    :class:`XtQuantLiveAdapter` uses (constructor with
    ``path`` / ``session_id``, ``register_callback``, ``start``,
    ``connect``, ``subscribe``, ``order_stock``, ``cancel_order``,
    ``query_stock_orders``, ``query_stock_trades``,
    ``query_stock_positions``, ``query_stock_asset``).
  * Maintains an in-memory book of orders / trades / positions /
    asset so query methods return realistic data.
  * Exposes ``emit_on_*`` methods for tests to drive callbacks
    synchronously (the real SDK fires callbacks on its own
    background thread; tests don't need that complexity).
  * Has knobs (``fail_next_connect``, ``latency``,
    ``silent_disconnect_after``) to simulate failure modes the
    real SDK can produce.

**Not in scope**: matching the entire xtquant SDK. We only
implement the methods the adapter touches. If the adapter grows
a new SDK dependency, add the corresponding fake here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import ClassVar

from execution.brokers.xtquant_models import (
    XT_ORDER_STATUS_CANCELLED,
    XT_ORDER_STATUS_FILLED,
    XT_ORDER_STATUS_REPORTED,
    XT_ORDER_STATUS_SUBMITTED,
    XtAsset,
    XtOrder,
    XtPosition,
    XtTrade,
)

__all__ = [
    "FAKE_XT_DEFAULT_CASH",
    "FakeXtQuantTrader",
]


FAKE_XT_DEFAULT_CASH: float = 1_000_000.0


# ---------------------------------------------------------------------------
# Internal mutable book (not exposed; tests interact via emit_* / query_*).
# ---------------------------------------------------------------------------


@dataclass
class _OrderBook:
    """Mutable in-memory order store.

    Kept private to ``xtquant_fake.py``; tests don't construct
    directly — they drive via ``emit_*`` and read via
    ``query_stock_orders`` (which returns our :class:`XtOrder` shape).
    """

    next_order_id: int = 1
    by_id: dict[int, XtOrder] = field(default_factory=dict)
    by_remark: dict[str, int] = field(default_factory=dict)
    trades: list[XtTrade] = field(default_factory=list)


# ---------------------------------------------------------------------------
# FakeXtQuantTrader
# ---------------------------------------------------------------------------


class FakeXtQuantTrader:
    """Stand-in for ``xtquant.xttrader.XtQuantTrader``.

    Construction parameters mirror the real SDK's constructor
    signature (positional ``path`` first, then ``session_id``) so
    test factories look the same as production ones.
    """

    # xtconstant values (mirrored so tests can build orders without
    # importing xtquant):
    _STOCK_BUY: ClassVar[int] = 23
    _STOCK_SELL: ClassVar[int] = 24
    _FIX_PRICE: ClassVar[int] = 11
    _MARKET_PRICE: ClassVar[int] = 12

    def __init__(self, path: str = "", session_id: int = 0) -> None:
        self.path = path
        self.session_id = session_id

        # Lifecycle counters (tests assert these for "did the
        # adapter call connect / disconnect the right number of
        # times?").
        self.connect_count = 0
        self.disconnect_count = 0
        self.subscribe_count = 0
        self.connect_rc = 0  # last connect() return code

        # Connection state.
        self._connected = False
        self._subscribed = False
        self._callback = None

        # Mutable book.
        self._book = _OrderBook()

        # Account state. Defaults make the fake usable out of the box
        # without per-test setup.
        self._asset = XtAsset(
            account_id="fake-account",
            cash=FAKE_XT_DEFAULT_CASH,
            frozen_cash=0.0,
            market_value=0.0,
            total_asset=FAKE_XT_DEFAULT_CASH,
            available_cash=FAKE_XT_DEFAULT_CASH,
        )
        self._positions: list[XtPosition] = []

        # Test knobs.
        self.fail_next_connect: bool = False
        """If True, the next ``connect()`` call returns rc=1 and
        does NOT transition to connected. Auto-resets after one
        failed call."""
        self.latency_seconds: float = 0.0
        """Sleep this many seconds in ``order_stock`` and ``query_*``
        (simulates slow broker). Default 0 for fast tests."""

    # ---------- Lifecycle ----------

    def register_callback(self, callback: object) -> None:
        """Set the callback (mirrors real SDK; real type is
        ``XtQuantTraderCallback`` but we accept ``object`` so tests
        can substitute a plain class)."""
        self._callback = callback

    def start(self) -> None:
        """No-op in the fake (real SDK spawns a background thread)."""
        return None

    def connect(self) -> int:
        """Return ``0`` on success, non-zero on failure.

        Idempotent: re-connecting when already connected is a
        no-op (does NOT increment ``connect_count``).

        With ``fail_next_connect=True``, returns ``1`` and stays
        disconnected. Otherwise transitions to connected and
        returns ``0``.
        """
        if self.fail_next_connect:
            self.fail_next_connect = False
            self.connect_rc = 1
            return 1
        if self._connected:
            # Idempotent re-call — don't double-count.
            return 0
        self._connected = True
        self.connect_count += 1
        self.connect_rc = 0
        return 0

    def disconnect(self) -> None:
        """Mark disconnected. Idempotent: disconnecting when not
        connected is a no-op (does NOT increment disconnect_count)."""
        if not self._connected:
            return
        self._connected = False
        self._subscribed = False
        self.disconnect_count += 1

    def subscribe(self, account: object) -> int:
        """Mark subscribed. Real SDK takes a ``StockAccount``; we
        accept ``object`` to avoid an import."""
        self._subscribed = True
        self.subscribe_count += 1
        return 0

    # ---------- Order placement ----------

    def order_stock(
        self,
        account: object,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int,
        price: float,
        strategy_name: str = "",
        order_remark: str = "",
    ) -> int:
        """Place an order. Returns a broker-assigned ``order_id``.

        Idempotent on ``order_remark``: a duplicate remark returns
        the existing ``order_id``. Mirrors the real xtquant behavior
        (no native ``client_order_id``; the convention is remark-
        encoded, which the adapter relies on).
        """
        if self.latency_seconds:
            time.sleep(self.latency_seconds)
        if not self._connected:
            raise RuntimeError("fake trader not connected")
        # Idempotent on remark.
        if order_remark and order_remark in self._book.by_remark:
            return self._book.by_remark[order_remark]
        new_id = self._book.next_order_id
        self._book.next_order_id += 1
        order = XtOrder(
            order_id=new_id,
            order_remark=order_remark,
            stock_code=stock_code,
            order_type=order_type,
            price_type=price_type,
            order_volume=order_volume,
            price=price,
            traded_volume=0,
            traded_price=0.0,
            order_status=XT_ORDER_STATUS_SUBMITTED,
            order_time="2026-09-02T09:30:00",
        )
        self._book.by_id[new_id] = order
        if order_remark:
            self._book.by_remark[order_remark] = new_id
        # Fire on_order asynchronously — but we're in the same
        # thread as the test, so do it inline. The real SDK fires
        # this on its background thread; tests get to see it
        # immediately.
        if self._callback is not None:
            self._callback.on_order(order)
        return new_id

    def cancel_order(self, account: object, order_id: int) -> None:
        """Cancel an order. Idempotent on already-cancelled ids."""
        order = self._book.by_id.get(order_id)
        if order is None:
            return None
        cancelled = XtOrder(
            order_id=order.order_id,
            order_remark=order.order_remark,
            stock_code=order.stock_code,
            order_type=order.order_type,
            price_type=order.price_type,
            order_volume=order.order_volume,
            price=order.price,
            traded_volume=order.traded_volume,
            traded_price=order.traded_price,
            order_status=XT_ORDER_STATUS_CANCELLED,
            order_time=order.order_time,
        )
        self._book.by_id[order_id] = cancelled
        if self._callback is not None:
            self._callback.on_order(cancelled)
        return None

    # ---------- Query methods ----------

    def query_stock_orders(
        self, account: object, cancelable_only: bool = False
    ) -> list[XtOrder] | None:
        """Return all known orders. Mirrors the real SDK's
        ``cancelable_only`` flag."""
        if self.latency_seconds:
            time.sleep(self.latency_seconds)
        if not self._connected:
            return None
        orders = list(self._book.by_id.values())
        if cancelable_only:
            orders = [
                o
                for o in orders
                if o.order_status in (XT_ORDER_STATUS_SUBMITTED, XT_ORDER_STATUS_REPORTED)
            ]
        return orders

    def query_stock_order(self, account: object, order_id: int) -> XtOrder | None:
        """Return one order by id, or None if not found."""
        if self.latency_seconds:
            time.sleep(self.latency_seconds)
        return self._book.by_id.get(order_id)

    def query_stock_trades(self, account: object) -> list[XtTrade] | None:
        if self.latency_seconds:
            time.sleep(self.latency_seconds)
        if not self._connected:
            return None
        return list(self._book.trades)

    def query_stock_positions(self, account: object) -> list[XtPosition] | None:
        if self.latency_seconds:
            time.sleep(self.latency_seconds)
        if not self._connected:
            return None
        return list(self._positions)

    def query_stock_asset(self, account: object) -> XtAsset | None:
        if self.latency_seconds:
            time.sleep(self.latency_seconds)
        if not self._connected:
            return None
        return self._asset

    # ---------- Test driver methods ----------
    #
    # These methods exist ONLY for tests. Production code never calls
    # them — they simulate events the real SDK would push via
    # ``XtQuantTradeCallback`` on its background thread.

    def emit_on_trade(
        self,
        *,
        order_id: int,
        stock_code: str,
        direction: int,
        volume: int,
        price: float,
        time_str: str = "2026-09-02T09:30:00",
    ) -> None:
        """Push an on_trade callback.

        Updates the order's ``traded_volume`` / ``traded_price`` so
        subsequent ``query_stock_orders`` reflects the fill.
        Mirrors what the real SDK does internally.
        """
        order = self._book.by_id.get(order_id)
        if order is None:
            return
        new_traded_vol = order.traded_volume + volume
        avg_price = (
            (order.traded_price * order.traded_volume + price * volume) / new_traded_vol
            if new_traded_vol > 0
            else 0.0
        )
        new_status = (
            XT_ORDER_STATUS_FILLED if new_traded_vol >= order.order_volume else order.order_status
        )
        updated = XtOrder(
            order_id=order.order_id,
            order_remark=order.order_remark,
            stock_code=order.stock_code,
            order_type=order.order_type,
            price_type=order.price_type,
            order_volume=order.order_volume,
            price=order.price,
            traded_volume=new_traded_vol,
            traded_price=avg_price,
            order_status=new_status,
            order_time=order.order_time,
        )
        self._book.by_id[order_id] = updated

        trade = XtTrade(
            order_id=order_id,
            stock_code=stock_code,
            direction=direction,
            traded_volume=volume,
            traded_price=price,
            traded_amount=volume * price,
            traded_time=time_str,
        )
        self._book.trades.append(trade)
        if self._callback is not None:
            # The real SDK fires BOTH on_order (status update) and
            # on_trade (fill event) for each trade. Mirror that.
            on_trade = getattr(self._callback, "on_trade", None)
            if on_trade is not None:
                on_trade(trade)
            if new_status != order.order_status:
                on_order = getattr(self._callback, "on_order", None)
                if on_order is not None:
                    on_order(updated)

    def emit_on_order_status(
        self,
        *,
        order_id: int,
        status: int,
    ) -> None:
        """Push a status-only on_order update (no fill)."""
        order = self._book.by_id.get(order_id)
        if order is None:
            return
        updated = XtOrder(
            order_id=order.order_id,
            order_remark=order.order_remark,
            stock_code=order.stock_code,
            order_type=order.order_type,
            price_type=order.price_type,
            order_volume=order.order_volume,
            price=order.price,
            traded_volume=order.traded_volume,
            traded_price=order.traded_price,
            order_status=status,
            order_time=order.order_time,
        )
        self._book.by_id[order_id] = updated
        if self._callback is not None:
            on_order = getattr(self._callback, "on_order", None)
            if on_order is not None:
                on_order(updated)

    def emit_on_disconnected(self) -> None:
        """Fire ``on_disconnected`` to simulate a TCP drop."""
        if self._callback is not None:
            on_disconnected = getattr(self._callback, "on_disconnected", None)
            if on_disconnected is not None:
                on_disconnected()

    def set_asset(self, asset: XtAsset) -> None:
        """Inject an asset snapshot (for tests asserting on
        query_account)."""
        self._asset = asset

    def set_positions(self, positions: list[XtPosition]) -> None:
        """Inject positions (for tests asserting on query_positions)."""
        self._positions = list(positions)

    def reset(self) -> None:
        """Reset book + counters. Use between phases of one test."""
        self._book = _OrderBook()
        self._asset = XtAsset(
            account_id="fake-account",
            cash=FAKE_XT_DEFAULT_CASH,
            frozen_cash=0.0,
            market_value=0.0,
            total_asset=FAKE_XT_DEFAULT_CASH,
            available_cash=FAKE_XT_DEFAULT_CASH,
        )
        self._positions = []
        self.connect_count = 0
        self.disconnect_count = 0
        self.subscribe_count = 0
        self._connected = False
        self._subscribed = False

    # Convenience: re-exported constants so callers don't need to
    # import xtquant_models separately for the fake.
    STOCK_BUY: ClassVar[int] = 23
    STOCK_SELL: ClassVar[int] = 24
    FIX_PRICE: ClassVar[int] = 11
    MARKET_PRICE: ClassVar[int] = 12
