"""BrokerAdapter abstract Protocol (W7.1).

This Protocol is the single contract every broker implementation
must satisfy:

  * :mod:`execution.brokers.akquant_paper` — default Phase 1
  * :mod:`execution.brokers.xtquant_live` — Phase 2 stub
  * Future adapters (simulated, mock, paper-via-ib, etc.)

The runner only sees this Protocol. Concrete classes (AKQuant,
XtQuant, custom) are registered by name into
:class:`execution.brokers.registry.BrokerRegistry` and instantiated
on demand — see the registry module.

Why Protocol, not ABC: Protocol is structural (duck-typed). Test
fakes don't need to subclass; they just need the right method
shapes. This matches how AKQuant itself ships its own Protocol
types (``akquant.gateway.protocols.TraderGateway``).
"""

from __future__ import annotations

from typing import Protocol

from execution.protocol import (
    EquitySnapshot,
    ExecutionReport,
    OrderIntent,
    Position,
)

__all__ = ["BrokerAdapter"]


class BrokerAdapter(Protocol):
    """Broker execution surface.

    Every method is **synchronous** for Phase 1 (AKQuant stub is
    blocking; XtQuant's blocking ``order_stock`` is also covered).
    Phase 2 may add ``async`` siblings or push-event hooks for
    the live callbacks (``on_order``, ``on_trade``), but the
    runner-facing surface stays sync to keep the journal/runner
    loop simple.

    Attributes:
        name: Adapter identifier (e.g. ``"akquant_paper"``,
            ``"xtquant_live"``). Set by the concrete class. Used
            in journal + log messages.
    """

    name: str

    def connect(self) -> None:
        """Open the broker session.

        Implementations should be idempotent: a second ``connect``
        after a successful one is a no-op (does NOT raise).
        """

    def disconnect(self) -> None:
        """Close the broker session.

        Idempotent: disconnecting when not connected is a no-op.
        """

    def place_order(self, intent: OrderIntent) -> ExecutionReport:
        """Submit an order and return its execution report.

        Implementations MUST return an :class:`ExecutionReport`
        whose ``status`` is one of ``{"submitted", "filled",
        "partial", "rejected", "cancelled"}`. ``status="submitted"``
        is acceptable for brokers that don't fill synchronously;
        the runner treats it as "intent accepted but not yet
        executed" (paper mode fills synchronously, so this is
        only relevant for live brokers).

        Implementations MUST be idempotent on
        ``intent.client_order_id``: a duplicate submission returns
        the existing broker_order_id rather than creating a new
        order. AKQuant enforces this via its stub's
        ``enforce_client_order_id_uniqueness`` flag.
        """

    def cancel_order(self, broker_order_id: str) -> ExecutionReport:
        """Cancel an open order by broker id.

        For order ids that have already filled or never existed,
        implementations MUST return a report with
        ``status="cancelled"`` or ``"rejected"`` (not raise).
        """

    def query_positions(self) -> list[Position]:
        """Return current positions.

        For paper / no-broker cases, return an empty list.
        """

    def query_account(self) -> EquitySnapshot:
        """Return the latest equity snapshot from the broker.

        Implementations MAY use a cached value rather than hitting
        the broker on every call (XtQuant ``query_stock_asset``
        is sync but slow). The runner caches the snapshot via
        the journal anyway, so this is called once per bar at
        most.
        """
