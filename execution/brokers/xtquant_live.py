"""Phase 2 stub for the XtQuant / miniQMT live adapter.

Per the 2026-08-31 design session, W7.1 ships this as a
``NotImplementedError`` stub so the :class:`BrokerAdapter` Protocol
is locked in but no live-trading code actually runs yet.

**When Phase 2 starts**, replace each method body with the
implementation sketched in the comments. The contract — what
``runner.py`` expects — will not change.

## Phase 2 implementation skeleton (for future reference)

::

    from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
    from xtquant.xttype import StockAccount
    from xtquant import xtconstant

    class XtQuantLiveAdapter:
        name = "xtquant_live"

        def __init__(self, *, path: str, session_id: int, account_id: str,
                     account_type: str = "STOCK"):
            self._path = path
            self._session_id = session_id
            self._account = StockAccount(account_id, account_type)
            self._trader = XtQuantTrader(path=path, session_id=session_id)
            self._callbacks = _BrokerCallbackQueue()   # queues push events
            self._trader.register_callback(self._callbacks)
            self._client_to_broker: dict[str, int] = {}
            self._broker_to_client: dict[int, str] = {}

        def connect(self):
            self._trader.start()
            rc = self._trader.connect()
            if rc != 0:
                raise RuntimeError(f"xtquant connect failed: rc={rc}")
            self._trader.subscribe(self._account)

        def place_order(self, intent: OrderIntent) -> ExecutionReport:
            # Encode client_order_id into order_remark (xtquant
            # has no native client_order_id).
            remark = f"q:{intent.client_order_id}"
            stock_code = _symbol_to_xtcode(intent.symbol)   # 6-digit → "000001.SZ"
            oid = self._trader.order_stock(
                self._account,
                stock_code,
                xtconstant.STOCK_BUY if intent.side == "buy" else xtconstant.STOCK_SELL,
                intent.quantity,
                xtconstant.FIX_PRICE,
                intent.price or 0.0,
                strategy_name="execution",
                order_remark=remark,
            )
            self._client_to_broker[intent.client_order_id] = oid
            self._broker_to_client[oid] = intent.client_order_id
            return ExecutionReport(
                client_order_id=intent.client_order_id,
                broker_order_id=str(oid),
                status="submitted",
                timestamp=utcnow(),
            )

        def on_disconnected_handler(self):
            # Phase 2: trigger watchdog — exponential backoff
            # reconnect + reconciliation via query_stock_orders/
            # trades/positions/asset. See plan section "Why" for
            # the full list of failure modes.
            ...

**Important Phase 2 constraints** (from W7.1 explore):

  * xtquant callbacks run on SDK background thread. The runner
    must consume events via a ``queue.Queue``; NEVER block the
    callback thread (it desyncs subsequent events).
  * No native ``client_order_id`` → encode in ``order_remark``
    with a sentinel prefix (``q:<uuid>``) and parse back on
    ``on_order`` / ``on_trade``.
  * ``on_disconnected`` does NOT fire on silent TCP half-opens.
    Implement a heartbeat watchdog (``last_received_tick``) to
    detect dead sessions.
  * On reconnect, always call ``subscribe(account)`` again —
    ``connect() == 0`` does NOT mean subscription is alive.
"""

from __future__ import annotations

from typing import Final

from execution.protocol import (
    EquitySnapshot,
    ExecutionReport,
    OrderIntent,
    Position,
    utcnow,
)

__all__ = ["XtQuantLiveAdapter"]


_NOT_IMPLEMENTED_MSG: Final[str] = (
    "XtQuantLiveAdapter is a Phase 2 stub. W7.1 (this commit) ships "
    "only the AkquantPaperAdapter. See execution/brokers/xtquant_live.py "
    "docstring for the Phase 2 implementation skeleton."
)


class XtQuantLiveAdapter:
    """Phase 2 stub. Every method raises ``NotImplementedError``.

    The class IS registered in :mod:`execution.brokers.registry`
    so ``create_registered_broker("xtquant_live")`` succeeds —
    callers that try to use it will get a clear error pointing
    at the Phase 2 plan.

    Attributes:
        name: Always ``"xtquant_live"`` (used by registry tests
            and runner introspection).
    """

    name: str = "xtquant_live"

    def connect(self) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def disconnect(self) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def place_order(self, intent: OrderIntent) -> ExecutionReport:
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def cancel_order(self, broker_order_id: str) -> ExecutionReport:
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def query_positions(self) -> list[Position]:
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def query_account(self) -> EquitySnapshot:
        # Return a zero snapshot so introspection / dashboard
        # queries don't crash before the Phase 2 fill-in lands.
        # Tests that actually need account state use the paper
        # adapter.
        return EquitySnapshot(
            timestamp=utcnow(),
            cash=0.0,
            positions_value=0.0,
            total_equity=0.0,
            drawdown_pct=0.0,
        )
