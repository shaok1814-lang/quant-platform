"""Broker adapter registry + base protocol (W7.1).

This sub-package owns the gateway surface. The runner speaks
:class:`BrokerAdapter` (defined here) and never imports AKQuant or
xtquant directly — that's the whole point of the abstraction.

Phase 1 (this commit):

  * :mod:`execution.brokers.base` — ``BrokerAdapter`` Protocol
  * :mod:`execution.brokers.akquant_paper` — wraps
    ``akquant.gateway.brokers.miniqmt.stub.MiniQMTTraderGateway``
    as the default paper backend. No xtquant / no miniQMT client
    required to run.
  * :mod:`execution.brokers.xtquant_live` — Phase 2 stub. Every
    method raises ``NotImplementedError`` so the interface is
    locked in even before the live implementation lands.

Phase 2 will:

  * Fill in ``XtQuantLiveAdapter`` (callback subclass, client_order_id
    via ``order_remark``, on_disconnected watchdog, reconciliation
    on reconnect).
  * Wire ``XtQuantLiveAdapter`` into ``BrokerRegistry`` so the
    runner can pick it via ``registry.create("xtquant_live", ...)``.
"""

from execution.brokers.base import BrokerAdapter
from execution.brokers.registry import (
    BrokerRegistry,
    create_registered_broker,
    list_registered_brokers,
    register_broker,
)

__all__ = [
    "BrokerAdapter",
    "BrokerRegistry",
    "create_registered_broker",
    "list_registered_brokers",
    "register_broker",
]
