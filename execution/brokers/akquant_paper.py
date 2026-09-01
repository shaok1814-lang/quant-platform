"""Paper backend wrapping AKQuant's MiniQMTTraderGateway stub (W7.1).

AKQuant ships ``akquant.gateway.brokers.miniqmt.stub.MiniQMTTraderGateway``
as an **in-memory stub** that satisfies the :class:`TraderGateway`
Protocol without importing xtquant. This makes it a perfect paper-mode
backend for development on machines that:

  * have AKQuant installed (we already do — pinned in pyproject.toml)
  * do NOT have xtquant installed (Windows-only DLL)
  * do NOT have a miniQMT client / broker account

How the wrapper works:

  1. Translate :class:`OrderIntent` → AKQuant's ``UnifiedOrderRequest``
     (``side``: ``"buy"/"sell"`` passes through; ``order_type``:
     ``"limit"/"market"`` → ``"Limit"/"Market"``).
  2. Call ``gateway.place_order(req)`` → returns broker_order_id.
  3. **Simulate a synchronous fill** at the intent's limit price
     (or market price; we don't model slippage in Phase 1).
  4. Update internal ``self._positions`` and ``self._cash`` to reflect
     the fill.
  5. Translate back to our :class:`ExecutionReport` /
     :class:`Position` / :class:`EquitySnapshot` shapes.

The AKQuant stub's internal state (``gateway.orders``, ``gateway.trades``)
is left alone — we maintain our own state in the wrapper because:

  * The stub's ``query_positions`` always returns ``[]`` (hard-coded)
  * The stub's ``query_account`` returns kwargs-driven defaults
    (we want to track real fills-driven equity for journal truth)
  * Pulling ``broker_order_id`` mappings out of the stub is brittle
    (relies on private attributes)

State invariants:

  * ``self._cash`` is updated on every fill (buy subtracts price * qty + commission,
    sell adds price * qty minus commission minus stamp_tax).
  * ``self._positions[symbol]`` is updated on every fill (avg-cost
    recalculated on buys; realized PnL recorded on sells).
  * ``self._high_water_mark`` updated after each ``query_account``
    so drawdown_pct reflects the lifetime of THIS session.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from execution.protocol import (
    EquitySnapshot,
    ExecutionReport,
    Fill,
    OrderIntent,
    Position,
    Side,
    utcnow,
)

__all__ = [
    "DEFAULT_INITIAL_EQUITY",
    "AkquantPaperAdapter",
]


DEFAULT_INITIAL_EQUITY: Final[float] = 1_000_000.0


def _normalize_side(side: Side) -> str:
    """Map our side literal to AKQuant's canonical lowercase string.

    Both are already ``"buy"`` / ``"sell"`` — this is a no-op kept
    as an indirection so Phase 2 can override if xtquant's side
    string differs (XtQuant uses ``xtconstant.STOCK_BUY`` = 23
    and ``xtconstant.STOCK_SELL`` = 24, NOT strings).
    """
    return side


def _normalize_order_type(order_type: str) -> str:
    """``"limit"`` → ``"Limit"``, ``"market"`` → ``"Market"``.

    AKQuant's ``UnifiedOrderRequest.order_type`` is documented to
    take the capitalized forms. The mapper in
    ``akquant.gateway.broker_event_adapter`` also accepts lowercase
    at the event-side, but the request construction is strict.
    """
    if order_type == "limit":
        return "Limit"
    if order_type == "market":
        return "Market"
    raise ValueError(f"unknown order_type: {order_type!r}")


class AkquantPaperAdapter:
    """Paper backend wrapping AKQuant's MiniQMTTraderGateway stub.

    Args:
        initial_cash: Starting cash in RMB. Default
            :data:`DEFAULT_INITIAL_EQUITY` (1M).
        commission_rate: Commission rate (default 0.0003 = 万 3).
        stamp_tax_rate: Stamp tax rate (default 0.001 = 千 1,
            sell-side only).
        initial_equity: Starting total equity. ``None`` defaults
            to ``initial_cash`` (no positions).
        enforce_client_order_id_uniqueness: Pass-through to AKQuant
            stub. Default ``True`` so duplicate submissions are
            idempotent (return existing broker_order_id).
        fill_at: If ``"limit"`` (default), limit orders fill at the
            intent's limit price. ``"market"`` requires a price
            (runner enforces this).
    """

    name: str = "akquant_paper"

    def __init__(
        self,
        *,
        initial_cash: float = DEFAULT_INITIAL_EQUITY,
        commission_rate: float = 0.0003,
        stamp_tax_rate: float = 0.001,
        initial_equity: float | None = None,
        enforce_client_order_id_uniqueness: bool = True,
        fill_at: str = "limit",
    ) -> None:
        self.initial_cash = initial_cash
        self.commission_rate = commission_rate
        self.stamp_tax_rate = stamp_tax_rate
        self.fill_at = fill_at
        self._initial_equity = initial_equity if initial_equity is not None else initial_cash
        self._high_water_mark: float = self._initial_equity

        # Lazy import so AKQuant is only required when this adapter
        # is actually instantiated (not when the package is imported).
        from akquant.gateway.broker_models import (
            UnifiedOrderRequest,
        )
        from akquant.gateway.brokers.miniqmt.stub import (
            MiniQMTTraderGateway,
        )

        self._UnifiedOrderRequest = UnifiedOrderRequest
        self._gateway = MiniQMTTraderGateway(
            account_id="paper",
            equity=self._initial_equity,
            cash=self.initial_cash,
            available_cash=self.initial_cash,
            enforce_client_order_id_uniqueness=enforce_client_order_id_uniqueness,
        )

        # Local state (kept here, not in the stub, for the reasons
        # in the module docstring).
        self._cash: float = initial_cash
        self._positions: dict[str, Position] = {}
        self._connected: bool = False
        # Set of client_order_ids we've already recorded a fill for.
        # AKQuant's stub enforces client_order_id uniqueness at the
        # gateway layer (returns the existing broker_order_id on
        # duplicate submissions), but the wrapper would otherwise
        # double-count the fill. Tracking here ensures exactly-once
        # fill semantics for idempotent resubmits.
        self._filled_client_ids: set[str] = set()

    # ---------- lifecycle ----------

    def connect(self) -> None:
        """Idempotent — second call after success is a no-op."""
        if self._connected:
            return
        self._gateway.connect()
        self._connected = True

    def disconnect(self) -> None:
        """Idempotent — disconnects even mid-session (no fill rollback)."""
        if not self._connected:
            return
        self._gateway.disconnect()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ---------- place / cancel ----------

    def place_order(self, intent: OrderIntent) -> ExecutionReport:
        """Submit + synchronously simulate a fill.

        Returns:
            :class:`ExecutionReport` with ``status="filled"`` for
            normal cases; ``"rejected"`` for invalid intents (no
            price on a limit order, non-positive quantity).
        """
        # Defensive validation — the runner should already have
        # enforced these, but the adapter is the last line.
        if intent.quantity <= 0:
            return ExecutionReport(
                client_order_id=intent.client_order_id,
                status="rejected",
                reject_reason=f"non-positive quantity {intent.quantity}",
                timestamp=utcnow(),
            )
        if intent.price is None or intent.price <= 0:
            return ExecutionReport(
                client_order_id=intent.client_order_id,
                status="rejected",
                reject_reason=f"missing or non-positive price {intent.price}",
                timestamp=utcnow(),
            )

        # Translate → submit via AKQuant stub.
        req = self._UnifiedOrderRequest(
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            side=_normalize_side(intent.side),
            quantity=float(intent.quantity),
            price=float(intent.price),
            order_type=_normalize_order_type(intent.order_type),
            time_in_force="GTC",
            position_effect="auto",
            asset_type="stock",
        )
        broker_order_id = self._gateway.place_order(req)

        # Idempotent re-submission guard. AKQuant's stub returns
        # the EXISTING broker_order_id for duplicate client_order_ids
        # (when enforce_client_order_id_uniqueness=True), so a
        # duplicate call here should NOT record a second fill —
        # that would double-count cash + position updates.
        if intent.client_order_id in self._filled_client_ids:
            return ExecutionReport(
                client_order_id=intent.client_order_id,
                broker_order_id=broker_order_id,
                status="filled",
                filled_quantity=intent.quantity,
                avg_fill_price=float(intent.price),
                timestamp=utcnow(),
            )

        # Synchronously simulate the fill at the intent's price.
        # Phase 2 may add slippage / partial-fill simulation here.
        report = self._record_fill(
            intent=intent,
            broker_order_id=broker_order_id,
            fill_price=float(intent.price),
            fill_quantity=intent.quantity,
            timestamp=utcnow(),
        )
        self._filled_client_ids.add(intent.client_order_id)
        return report

    def cancel_order(self, broker_order_id: str) -> ExecutionReport:
        """Cancel an order by broker id.

        The AKQuant stub only stores orders in its internal
        ``self.orders`` dict; the wrapper doesn't track orders
        (it tracks fills + positions). So a cancel request for an
        unknown id returns a ``"rejected"`` report rather than
        touching the gateway.
        """
        # Forward to the gateway so its internal state stays in
        # sync (it CANCELLED the snapshot). The wrapper's own
        # state is fill-based, so a successful cancel here only
        # matters if the broker id corresponds to a still-open
        # order in the gateway — which in paper mode is never
        # (we fill synchronously).
        snap = self._gateway.query_order(broker_order_id)
        if snap is None:
            return ExecutionReport(
                client_order_id=broker_order_id,
                broker_order_id=broker_order_id,
                status="rejected",
                reject_reason="unknown broker_order_id",
                timestamp=utcnow(),
            )
        self._gateway.cancel_order(broker_order_id)
        return ExecutionReport(
            client_order_id=snap.client_order_id,
            broker_order_id=broker_order_id,
            status="cancelled",
            timestamp=utcnow(),
        )

    # ---------- queries ----------

    def query_positions(self) -> list[Position]:
        return list(self._positions.values())

    def query_account(self) -> EquitySnapshot:
        positions_value = sum(
            p.quantity * p.avg_cost  # paper: use cost-basis valuation
            for p in self._positions.values()
        )
        total_equity = self._cash + positions_value
        # Update high water mark first, then compute drawdown.
        self._high_water_mark = max(self._high_water_mark, total_equity)
        dd_pct = max(
            0.0,
            (self._high_water_mark - total_equity) / max(self._high_water_mark, 1e-6),
        )
        return EquitySnapshot(
            timestamp=utcnow(),
            cash=self._cash,
            positions_value=positions_value,
            total_equity=total_equity,
            drawdown_pct=dd_pct,
        )

    # ---------- internal: fill bookkeeping ----------

    def _record_fill(
        self,
        *,
        intent: OrderIntent,
        broker_order_id: str,
        fill_price: float,
        fill_quantity: int,
        timestamp: datetime,
    ) -> ExecutionReport:
        """Apply the simulated fill to local state and return a report.

        Updates ``self._cash`` and ``self._positions[symbol]``,
        computing commission + stamp_tax per CLAUDE.md contract.
        """
        notional = fill_price * fill_quantity
        commission = notional * self.commission_rate
        stamp_tax = notional * self.stamp_tax_rate if intent.side == "sell" else 0.0

        if intent.side == "buy":
            self._cash -= notional + commission
            self._apply_buy(intent.symbol, fill_quantity, fill_price)
        else:
            self._cash += notional - commission - stamp_tax
            self._apply_sell(intent.symbol, fill_quantity, fill_price)

        return ExecutionReport(
            client_order_id=intent.client_order_id,
            broker_order_id=broker_order_id,
            status="filled",
            filled_quantity=fill_quantity,
            avg_fill_price=fill_price,
            timestamp=timestamp,
        )

    def _apply_buy(self, symbol: str, qty: int, price: float) -> None:
        """Update positions dict for a buy fill."""
        cur = self._positions.get(symbol)
        if cur is None or cur.quantity == 0:
            self._positions[symbol] = Position(
                symbol=symbol,
                quantity=qty,
                avg_cost=price,
            )
            return
        # Weighted-average cost on adding to a long position.
        total_qty = cur.quantity + qty
        new_avg = (cur.quantity * cur.avg_cost + qty * price) / total_qty
        self._positions[symbol] = Position(
            symbol=symbol,
            quantity=total_qty,
            avg_cost=new_avg,
            realized_pnl=cur.realized_pnl,
            unrealized_pnl=cur.unrealized_pnl,
        )

    def _apply_sell(self, symbol: str, qty: int, price: float) -> None:
        """Update positions dict for a sell fill (closes long; no shorts)."""
        cur = self._positions.get(symbol)
        if cur is None or cur.quantity == 0:
            # Selling into nothing — paper mode allows this (e.g.
            # "test sell" before any buy). Result: a phantom short
            # we don't model; just no position update.
            return
        if qty > cur.quantity:
            # Sell more than held — cap to what we have (paper mode
            # does not allow shorting). The runner should have
            # already prevented this; cap defensively.
            qty = cur.quantity
        realized = (price - cur.avg_cost) * qty
        new_qty = cur.quantity - qty
        if new_qty == 0:
            # Closed flat: drop the position.
            self._positions.pop(symbol, None)
        else:
            self._positions[symbol] = Position(
                symbol=symbol,
                quantity=new_qty,
                avg_cost=cur.avg_cost,
                realized_pnl=cur.realized_pnl + realized,
                unrealized_pnl=cur.unrealized_pnl,
            )

    # ---------- convenience for tests / journal ----------

    def make_fill_record(
        self,
        intent: OrderIntent,
        report: ExecutionReport,
    ) -> Fill | None:
        """Build a :class:`Fill` from a freshly-submitted intent + report.

        The runner calls this after each ``place_order`` to get the
        journal row. Returns ``None`` for non-fill statuses.
        """
        if report.status not in ("filled", "partial"):
            return None
        if report.avg_fill_price is None or report.filled_quantity == 0:
            return None
        import uuid

        notional = report.avg_fill_price * report.filled_quantity
        commission = notional * self.commission_rate
        stamp_tax = notional * self.stamp_tax_rate if intent.side == "sell" else 0.0
        return Fill(
            fill_id=f"fill-{uuid.uuid4().hex}",
            client_order_id=intent.client_order_id,
            broker_order_id=report.broker_order_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=report.filled_quantity,
            price=report.avg_fill_price,
            commission=commission,
            stamp_tax=stamp_tax,
            timestamp=report.timestamp or utcnow(),
        )


# Internal state accessor for tests. Not part of the Protocol.
def _get_internal_state(adapter: AkquantPaperAdapter) -> dict[str, Any]:
    """Read-only view of the wrapper's private state. Test-only."""
    return {
        "cash": adapter._cash,
        "positions": dict(adapter._positions),
        "high_water_mark": adapter._high_water_mark,
    }
