"""Core data types for the execution layer (W7.1).

This module is the single source of truth for the dataclasses that
flow through the runner / risk / adapter / journal pipeline. Every
other module in ``execution/`` imports from here. Strategies emit
:class:`OrderIntent`, risk helpers return :class:`RiskDecision`,
adapters return :class:`ExecutionReport`, and the journal persists
:class:`Fill` + :class:`EquitySnapshot` rows.

Design choices:

* **Project-owned types** (not AKQuant's ``UnifiedOrderRequest`` /
  ``UnifiedTrade``). The wrapper in ``execution.brokers.akquant_paper``
  translates between our types and AKQuant's. Rationale per the
  2026-08-31 design session: independent abstraction makes the
  execution layer testable without instantiating AKQuant, and
  decouples us from AKQuant's gateway protocol evolution.

* **All dataclasses are ``frozen=True``**. Per CLAUDE.md 「测试要
  短路径」, immutability makes journal writes idempotent and
  snapshot equality trivially cheap.

* **A 股 specifics are NOT baked into the types** (no
  ``is_st`` / ``board`` flags). Those live in
  :mod:`backtest.a_share` and are the strategy's responsibility
  before it emits an ``OrderIntent``. Keeps the execution layer
  agnostic of market microstructure.

* **Timestamps** are :class:`datetime.datetime` (UTC, naive — no
  tzinfo). The runner and journal serialize as ISO strings; we do
  not pass ``datetime`` over AKQuant's nanosecond ``timestamp_ns``
  fields to avoid unit confusion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

__all__ = [
    "DEFAULT_COMMISSION_RATE",
    "DEFAULT_INITIAL_CASH",
    "DEFAULT_RISK_CONFIG",
    "DEFAULT_STAMP_TAX_RATE",
    "EquitySnapshot",
    "ExecutionReport",
    "ExecutionStatus",
    "Fill",
    "OrderIntent",
    "OrderType",
    "Position",
    "RiskConfig",
    "Side",
]


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

Side = Literal["buy", "sell"]
OrderType = Literal["limit", "market"]
ExecutionStatus = Literal[
    "submitted",  # broker acknowledged; no fill yet
    "filled",  # fully filled
    "partial",  # partially filled
    "rejected",  # broker refused (invalid price, symbol halted, etc.)
    "cancelled",  # we cancelled before fill
]


# ---------------------------------------------------------------------------
# Defaults (CLAUDE.md + market convention)
# ---------------------------------------------------------------------------

# Commission rate default: 0.0003 (万 3) is the standard A-share
# commission for most brokers (some have a 5 元 minimum, which is a
# Phase 2 concern; not in scope for W7.1 paper skeleton).
DEFAULT_COMMISSION_RATE: Final[float] = 0.0003

# Stamp tax default: 0.001 (千 1) sell-side only per CLAUDE.md.
DEFAULT_STAMP_TAX_RATE: Final[float] = 0.001

# Initial cash for paper sessions: 1M RMB. Easy round number; runner
# lets callers override via PaperSessionConfig.
DEFAULT_INITIAL_CASH: Final[float] = 1_000_000.0


# CLAUDE.md 「单策略初始实盘资金不超过总资金 10%」
# + 「单日 round-trip ≤ 20」 (avoid over-trading)
# + 「回撤 ≥ 5% 暂停」 (drawdown kill switch)
# Sentinel: replaced after RiskConfig is defined below. Avoids the
# forward-reference trap when constructing the default constant.
_DEFAULT_RISK_CONFIG_SENTINEL: Final[None] = None


# ---------------------------------------------------------------------------
# Order intent (strategy → risk → adapter)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderIntent:
    """A buy/sell instruction emitted by the strategy layer.

    Attributes:
        client_order_id: Unique per-intent ID. We generate this
            (uuid4) so adapters can be idempotent on retry. AKQuant
            enforces client_order_id uniqueness, so a duplicate
            submission returns the existing broker_order_id. XtQuant
            will encode this into ``order_remark`` in Phase 2.
        symbol: 6-digit A-share symbol, no exchange suffix
            (e.g. ``"000001"``, ``"600000"``). Adapters translate to
            full ``"<6-digit>.SH"`` / ``"<6-digit>.SZ"`` as needed.
        side: ``"buy"`` or ``"sell"``. Sell is **not** a short-sell
            signal at this layer (no A-share retail short support);
            it's "close existing long" semantics.
        quantity: Whole shares. The strategy should already have run
            :func:`backtest.a_share.lot_enforcement.enforce_lot` so
            this is a multiple of 100. The runner does NOT
            re-validate (kept thin).
        price: Limit price. ``None`` if ``order_type="market"``; the
            adapter treats ``None`` as 市价单.
        order_type: ``"limit"`` (default) or ``"market"``.
        reason: Free-text strategy-side note for journal debugging
            (e.g. ``"ma_cross_5_10_cross_up"``). Not interpreted by
            risk or adapter.
    """

    client_order_id: str
    symbol: str
    side: Side
    quantity: int
    price: float | None = None
    order_type: OrderType = "limit"
    reason: str = ""


# ---------------------------------------------------------------------------
# Execution report (adapter → runner)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionReport:
    """Result of :meth:`BrokerAdapter.place_order`.

    Attributes:
        client_order_id: Echoed from the intent.
        broker_order_id: Adapter-assigned id. For AKQuant stub this
            is ``"miniqmt-<client_id>-<seq>"``; for XtQuant it'll be
            the broker's :class:`XtOrder.order_id`. ``None`` only
            when the adapter rejects before sending (e.g. invalid
            symbol).
        status: Terminal (or intermediate) state. See
            :data:`ExecutionStatus`.
        filled_quantity: Shares filled. ``0`` if anything other than
            ``"filled"`` / ``"partial"``.
        avg_fill_price: VWAP of fills. ``None`` if no fills.
        reject_reason: Human-readable broker error string. ``None``
            on success.
        timestamp: UTC datetime when the adapter produced this
            report. ``None`` only if the adapter crashed before
            timestamping.
    """

    client_order_id: str
    status: ExecutionStatus
    broker_order_id: str | None = None
    filled_quantity: int = 0
    avg_fill_price: float | None = None
    reject_reason: str | None = None
    timestamp: datetime | None = None


# ---------------------------------------------------------------------------
# Portfolio state (snapshot / fill)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Position:
    """Single-symbol position snapshot.

    Attributes:
        symbol: 6-digit symbol.
        quantity: Positive = long. We do not support short positions
            in paper mode (no A-share retail shorting).
        avg_cost: Volume-weighted average fill price. ``0.0`` when
            quantity is 0.
        realized_pnl: Cumulative closed-trade PnL in RMB. Unaffected
            by current market price.
        unrealized_pnl: Mark-to-market PnL in RMB. ``0.0`` outside
            of an EquitySnapshot evaluation (computed by the runner).
    """

    symbol: str
    quantity: int
    avg_cost: float
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass(frozen=True)
class Fill:
    """One execution = one journal row.

    Attributes:
        fill_id: Unique per-fill ID (uuid4). Distinct from
            ``client_order_id`` because a single order may fill in
            multiple partials.
        client_order_id: FK to the originating intent.
        broker_order_id: FK to the broker's id; ``None`` only if
            the adapter failed before assignment.
        symbol: 6-digit symbol.
        side: ``"buy"`` or ``"sell"``.
        quantity: Shares filled in this fill event (not cumulative).
        price: Fill price (single event; partial fills would emit
            multiple :class:`Fill` rows).
        commission: RMB. ``0.0`` for sell-side if a runner chooses
            to fold commission into the broker's notional accounting;
            otherwise both sides per W4 contract.
        stamp_tax: RMB. ``0.0`` for buy-side (CLAUDE.md: 印花税
            卖出单边). The runner computes via
            :func:`backtest.a_share.stamp_tax.compute_stamp_tax`.
        timestamp: UTC datetime of the fill event.
    """

    fill_id: str
    client_order_id: str
    symbol: str
    side: Side
    quantity: int
    price: float
    timestamp: datetime
    broker_order_id: str | None = None
    commission: float = 0.0
    stamp_tax: float = 0.0


@dataclass(frozen=True)
class EquitySnapshot:
    """Portfolio valuation at a point in time.

    The runner records one per bar (configurable via
    :attr:`PaperSessionConfig.snapshot_every_n_bars`). Used to
    compute drawdown for the kill switch + 4-week paper replay.

    Attributes:
        timestamp: UTC datetime.
        cash: Free cash, RMB.
        positions_value: Sum of ``quantity * current_price`` across
            all open positions. ``0.0`` when flat.
        total_equity: ``cash + positions_value``.
        drawdown_pct: ``(high_water_mark - total_equity) / high_water_mark``.
            ``0.0`` if at or above the high-water-mark. Always
            non-negative.
    """

    timestamp: datetime
    cash: float
    positions_value: float
    total_equity: float
    drawdown_pct: float


# ---------------------------------------------------------------------------
# Risk configuration (CLAUDE.md hard constraints)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskConfig:
    """CLAUDE.md 「合规与实盘纪律」 three-rule container.

    Defaults match the project's documented hard limits; callers
    pass an instance with non-default values to tighten (e.g. a
    more conservative 5% position cap for early paper tests).

    Attributes:
        max_position_pct: Per-symbol position cap as a fraction of
            ``total_equity``. CLAUDE.md: 单策略初始实盘资金不超过
            总资金 10%. Default 0.10.
        max_daily_trades: Maximum round-trip count per trading day.
            Default 20 (an arbitrary conservative cap; the
            intent is to flag over-trading strategies during
            paper testing).
        drawdown_kill_switch_pct: Total-equity drawdown threshold.
            When ``EquitySnapshot.drawdown_pct >= this``, the runner
            refuses all new intents for the rest of the session.
            CLAUDE.md risk discipline: drawdown must trigger a
            stop. Default 0.05 (5%).
        enabled: Master switch. ``False`` disables all three checks
            (used by tests that want to bypass risk for fixture
            purposes). Default ``True``.
    """

    max_position_pct: float = 0.10
    max_daily_trades: int = 20
    drawdown_kill_switch_pct: float = 0.05
    enabled: bool = True
    enable_price_limit_guard: bool = True
    enable_suspension_guard: bool = True


# Replace the sentinel with the real default now that RiskConfig is
# defined. Use module-level __getattr__ would be cleaner but Python
# 3.11+ __getattr__ at module level is fiddly; this direct rebinding
# is the simplest equivalent.
DEFAULT_RISK_CONFIG: Final[RiskConfig] = RiskConfig()
del _DEFAULT_RISK_CONFIG_SENTINEL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """UTC ``datetime`` (naive, no tzinfo) — the canonical timestamp
    type for this package.

    Using naive UTC keeps SQLite string comparisons deterministic
    (``'2024-09-02T09:30:00' < '2024-09-02T09:31:00'`` is correct
    in ISO order without timezone arithmetic).
    """
    return datetime.now(UTC).replace(tzinfo=None)


def make_intent_id(prefix: str = "intent") -> str:
    """Generate a default client_order_id.

    Convenience for callers that don't want to import ``uuid``. The
    runner uses this when the strategy didn't pre-assign IDs.
    """
    import uuid

    return f"{prefix}-{uuid.uuid4().hex}"
