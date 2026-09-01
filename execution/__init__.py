"""Execution layer — paper-trade skeleton (W7.1).

This package implements the project's CLAUDE.md-mandated execution
layer:

  * :mod:`execution.protocol` — frozen dataclasses (OrderIntent,
    ExecutionReport, Position, Fill, EquitySnapshot, RiskConfig)
    flowing through the runner / risk / adapter / journal pipeline.
  * :mod:`execution.risk` — three pure-function guards
    (position cap, daily trade count, drawdown kill switch) with
    Allow/Reject sum-type returns.
  * :mod:`execution.journal` — SQLite-backed persistence for the
    4-week paper replay + (Phase 2) paper-vs-live deviation check.
  * :mod:`execution.runner` — :func:`run_paper_session` entry
    point driving the bar → strategy → risk → adapter → journal loop.
  * :mod:`execution.brokers` — broker adapter abstraction + two
    implementations: :class:`AkquantPaperAdapter` (Phase 1 default,
    no xtquant required) and :class:`XtQuantLiveAdapter` (Phase 2
    stub raising ``NotImplementedError``).

Usage (Phase 1)::

    from execution import (
        run_paper_session, OrderIntent, PaperJournal,
        AkquantPaperAdapter, RiskConfig,
    )

**What's NOT in this package** (Phase 2+):

  * Real miniQMT / xtquant live trading (the
    :class:`XtQuantLiveAdapter` stub is the placeholder).
  * Multi-strategy portfolio sessions.
  * Reconnect / heartbeat watchdog.
  * On-disconnect order reconciliation.
  * 钉聊 SOFT alerting on kill-switch events (Phase 2 hooks into
    ``ops.notify.ding``).
"""

from execution.brokers import (
    BrokerAdapter,
    BrokerRegistry,
    create_registered_broker,
    list_registered_brokers,
    register_broker,
)
from execution.journal import (
    CompareResult,
    DeviationRow,
    PaperJournal,
)
from execution.protocol import (
    DEFAULT_COMMISSION_RATE,
    DEFAULT_INITIAL_CASH,
    DEFAULT_RISK_CONFIG,
    DEFAULT_STAMP_TAX_RATE,
    EquitySnapshot,
    ExecutionReport,
    ExecutionStatus,
    Fill,
    OrderIntent,
    OrderType,
    Position,
    RiskConfig,
    Side,
    make_intent_id,
    utcnow,
)
from execution.risk import (
    Allow,
    Reject,
    RiskDecision,
)
from execution.runner import (
    AccountSlot,
    MultiAccountReport,
    PaperSessionConfig,
    PaperSessionReport,
    run_multi_account_paper_session,
    run_paper_session,
)


def __getattr__(name: str) -> object:
    """Lazy-import AkquantPaperAdapter / XtQuantLiveAdapter.

    These pull in AKQuant / xtquant respectively. Tests that use
    the package without those installed should import them
    directly from ``execution.brokers.akquant_paper`` etc.
    """
    if name == "AkquantPaperAdapter":
        from execution.brokers.akquant_paper import AkquantPaperAdapter

        return AkquantPaperAdapter
    if name == "XtQuantLiveAdapter":
        from execution.brokers.xtquant_live import XtQuantLiveAdapter

        return XtQuantLiveAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DEFAULT_COMMISSION_RATE",
    "DEFAULT_INITIAL_CASH",
    "DEFAULT_RISK_CONFIG",
    "DEFAULT_STAMP_TAX_RATE",
    "AccountSlot",
    "AkquantPaperAdapter",
    "Allow",
    "BrokerAdapter",
    "BrokerRegistry",
    "CompareResult",
    "DeviationRow",
    "EquitySnapshot",
    "ExecutionReport",
    "ExecutionStatus",
    "Fill",
    "MultiAccountReport",
    "OrderIntent",
    "OrderType",
    "PaperJournal",
    "PaperSessionConfig",
    "PaperSessionReport",
    "Position",
    "Reject",
    "RiskConfig",
    "RiskDecision",
    "Side",
    "XtQuantLiveAdapter",
    "create_registered_broker",
    "list_registered_brokers",
    "make_intent_id",
    "register_broker",
    "run_multi_account_paper_session",
    "run_paper_session",
    "utcnow",
]
