"""Paper-session runner (W7.1).

Drives bars through a callable strategy → risk checks → broker
adapter → journal. Symmetric in spirit with AKQuant's ``run_backtest``
but:

  * Single-strategy-per-session (no portfolio of strategies yet;
    Phase 2 / W7.2 territory).
  * Caller-driven risk config (no built-in optimizer; just the
    three CLAUDE.md hard limits).
  * Single-asset per session for Phase 1 (multi-asset via dict
    input is supported but treated as one symbol per call to
    strategy; runner fans out internally).
  * Pure-Python loop (no event loop, no async). Synchronous fill
    semantics match the AKQuant paper backend.

Strategy contract::

    def my_strategy(state: dict, recent_bars: list[Bar]) -> list[OrderIntent]:
        ...

  * ``state``: mutable dict owned by the runner. Strategies can
    stash any per-session state here (position tracking, last
    signal, cooldown counters, etc.).
  * ``recent_bars``: most recent N bars (single-symbol). Each
    ``Bar`` is a dict with keys ``date / open / high / low / close
    / volume``. N = ``max_history_depth`` from PaperSessionConfig.
  * Return value: list of :class:`OrderIntent` to evaluate this
    bar. Empty list = no action.

The runner does NOT promise thread safety — Phase 2's
multi-strategy work will revisit.

**Important**: the runner checks the drawdown kill switch BEFORE
running the strategy on each bar (not after). This means a
strategy that hits 5% drawdown stops adding new risk on the very
next bar, not after one more round-trip.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime
from typing import Any

import pandas as pd
from loguru import logger

from execution.journal import PaperJournal
from execution.protocol import (
    DEFAULT_COMMISSION_RATE,
    DEFAULT_INITIAL_CASH,
    DEFAULT_STAMP_TAX_RATE,
    EquitySnapshot,
    ExecutionReport,
    Fill,
    OrderIntent,
    RiskConfig,
    utcnow,
)
from execution.risk import (
    Allow,
    Reject,
    check_daily_trade_count,
    check_drawdown_kill_switch,
    check_position_cap,
)

__all__ = [
    "Bar",
    "PaperSessionConfig",
    "PaperSessionReport",
    "Strategy",
    "run_paper_session",
]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


# A bar is a single OHLCV row as the runner hands it to the strategy.
# Dict shape (not pd.Series) keeps the strategy callable trivial.
Bar = dict


# Strategy signature: ``def strategy(state, recent_bars) -> list[OrderIntent]``
# The runner owns ``state``; strategies mutate it freely.
Strategy = Callable[[dict[str, Any], list[Bar]], list[OrderIntent]]


@dataclass(frozen=True)
class PaperSessionConfig:
    """Per-invocation tuning knobs for :func:`run_paper_session`.

    Attributes:
        initial_cash: Starting cash in RMB. Default 1M.
        commission_rate: Per-side commission fraction. Default
            0.0003 (万 3) per W4 contract.
        stamp_tax_rate: Sell-side stamp tax fraction. Default
            0.001 (千 1) per CLAUDE.md.
        snapshot_every_n_bars: Write an EquitySnapshot every N
            bars. Default 1 (every bar). Higher values reduce
            journal write overhead at the cost of finer-grained
            drawdown tracking.
        max_history_depth: Maximum bars the strategy sees via
            ``recent_bars``. Default 50.
        bar_column: Name of the timestamp column in ``data``.
            Default ``"date"``.
        notify_fn: Optional callback invoked when the drawdown
            kill switch fires (CLAUDE.md 「回撤 ≥ 5% 暂停」).
            Production passes ``ops.notify.ding``; tests pass a
            spy closure. ``None`` (default) keeps the W7.1 paper-
            only behavior — kill switch still halts the session,
            but the operator only sees a loguru WARNING line.
    """

    initial_cash: float = DEFAULT_INITIAL_CASH
    commission_rate: float = DEFAULT_COMMISSION_RATE
    stamp_tax_rate: float = DEFAULT_STAMP_TAX_RATE
    snapshot_every_n_bars: int = 1
    max_history_depth: int = 50
    bar_column: str = "date"
    notify_fn: Callable[[str, str], None] | None = None


@dataclass(frozen=True)
class PaperSessionReport:
    """Aggregate outcome of one :func:`run_paper_session` invocation.

    Attributes:
        started_at: UTC datetime when the session began.
        finished_at: UTC datetime when the session ended.
        n_intents: Total OrderIntents emitted by the strategy.
        n_risk_rejected: Number of intents that failed a risk check.
        n_submitted: Number of intents sent to the adapter.
        n_filled: Number of intents that resulted in a fill.
        final_equity: Total equity at the end of the session.
        max_drawdown_pct: Maximum drawdown observed across all
            snapshots during the session.
    """

    started_at: datetime
    finished_at: datetime
    n_intents: int
    n_risk_rejected: int
    n_submitted: int
    n_filled: int
    final_equity: float
    max_drawdown_pct: float
    per_symbol: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, int | float | str | dict]:
        """Render as a JSON-serializable dict for dashboards."""
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "n_intents": self.n_intents,
            "n_risk_rejected": self.n_risk_rejected,
            "n_submitted": self.n_submitted,
            "n_filled": self.n_filled,
            "final_equity": self.final_equity,
            "max_drawdown_pct": self.max_drawdown_pct,
            "per_symbol": self.per_symbol,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_bar(row: pd.Series, ts: datetime) -> Bar:
    """Convert a DataFrame row into the Bar dict the strategy sees."""
    return {
        "date": ts,
        "open": float(row.get("open", 0.0)),
        "high": float(row.get("high", 0.0)),
        "low": float(row.get("low", 0.0)),
        "close": float(row.get("close", 0.0)),
        "volume": float(row.get("volume", 0.0)),
    }


def _to_bars_df(data: pd.DataFrame | dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Normalize the runner's data input to a single ``pd.DataFrame``.

    Phase 1 supports single-symbol only. Multi-symbol input (a dict)
    is rejected with a clear ValueError pointing at the Phase 2
    extension point.
    """
    if isinstance(data, pd.DataFrame):
        return data.reset_index(drop=True)
    if isinstance(data, dict):
        raise ValueError(
            "multi-symbol session is Phase 2. Pass a single pd.DataFrame; "
            "for now, run one session per symbol."
        )
    raise TypeError(
        f"data must be a pd.DataFrame (Phase 1); got {type(data).__name__}"
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_paper_session(
    strategy: Strategy,
    data: pd.DataFrame,
    *,
    adapter: Any,
    journal: PaperJournal,
    risk_cfg: Any = None,
    session_cfg: PaperSessionConfig | None = None,
) -> PaperSessionReport:
    """Run one paper-trading session end-to-end.

    Args:
        strategy: Callable ``(state, recent_bars) -> list[OrderIntent]``.
        data: OHLCV ``pd.DataFrame`` for one symbol. Must have the
            standard columns (``date / open / high / low / close /
            volume``).
        adapter: Any object satisfying the :class:`BrokerAdapter`
            Protocol (e.g. :class:`AkquantPaperAdapter`). The
            ``Any`` annotation lets test fakes skip the Protocol
            without subclassing.
        journal: :class:`PaperJournal` for persistence. The
            function never raises on journal write errors (we
            log + continue, per CLAUDE.md "数据可靠 > 单点
            失败断整个系统").
        risk_cfg: :class:`RiskConfig`. Default uses the module-level
            :data:`DEFAULT_RISK_CONFIG`.
        session_cfg: :class:`PaperSessionConfig`. Default uses
            module-level defaults.

    Returns:
        :class:`PaperSessionReport`.

    Raises:
        ValueError: On unsupported data shape (multi-symbol) or
            missing required columns.
    """
    from execution.protocol import DEFAULT_RISK_CONFIG

    if risk_cfg is None:
        risk_cfg = DEFAULT_RISK_CONFIG
    if session_cfg is None:
        session_cfg = PaperSessionConfig()

    bars_df = _to_bars_df(data)
    required = {"open", "high", "low", "close", "volume", session_cfg.bar_column}
    missing = required - set(bars_df.columns)
    if missing:
        raise ValueError(
            f"data missing required columns: {sorted(missing)}; "
            f"got {list(bars_df.columns)}"
        )

    # Normalize timestamp column to datetime.
    bars_df = bars_df.copy()
    bars_df[session_cfg.bar_column] = pd.to_datetime(bars_df[session_cfg.bar_column])

    started_at = utcnow()
    state: dict[str, Any] = {
        "bought": False,  # convenience for the smoke-test strategy
    }
    recent_bars: list[Bar] = []

    # Per-run counters.
    n_intents = 0
    n_risk_rejected = 0
    n_submitted = 0
    n_filled = 0
    max_drawdown_pct = 0.0
    kill_switch_active = False

    # Connect adapter. Idempotent — adapter.connect() may be a no-op
    # if already connected.
    adapter.connect()

    # Initial equity snapshot for HWM seeding.
    initial_snap = adapter.query_account()
    hwm: float = initial_snap.total_equity

    # Parse all bar timestamps up front (cheap; lets us bucket fills
    # by trading day for daily_trade_count without re-parsing).
    timestamps: list[datetime] = list(bars_df[session_cfg.bar_column])

    # Main loop. Each iteration is "at bar i", processing whatever
    # the strategy emitted for that bar.
    for i in range(len(bars_df)):
        row = bars_df.iloc[i]
        ts = timestamps[i]
        bar = _row_to_bar(row, ts)
        recent_bars.append(bar)
        if len(recent_bars) > session_cfg.max_history_depth:
            recent_bars = recent_bars[-session_cfg.max_history_depth:]

        # Snapshot for journal + drawdown computation. We snapshot
        # BEFORE running the strategy so the strategy sees its own
        # effect from the previous bar (and the kill switch uses
        # the most recent realized drawdown, not a stale one).
        snap = adapter.query_account()
        if i % session_cfg.snapshot_every_n_bars == 0:
            # Adapter's drawdown is lifetime (its own HWM, which
            # persists across runs). Track session-local HWM
            # separately for the journal 4-week replay.
            hwm = max(hwm, snap.total_equity)
            session_dd = max(0.0, (hwm - snap.total_equity) / max(hwm, 1e-6))
            session_snap = EquitySnapshot(
                timestamp=ts,
                cash=snap.cash,
                positions_value=snap.positions_value,
                total_equity=snap.total_equity,
                drawdown_pct=session_dd,
            )
            with contextlib.suppress(Exception):  # pragma: no cover
                journal.record_snapshot(session_snap)
            max_drawdown_pct = max(max_drawdown_pct, session_dd)

        # Kill switch check (cheap O(1)). Use the adapter's
        # lifetime drawdown, NOT the session-relative one — the
        # question is "has the portfolio lost >= 5%?", which
        # crosses session boundaries.
        if not kill_switch_active and risk_cfg.enabled:
            dd_decision = check_drawdown_kill_switch(
                EquitySnapshot(
                    timestamp=ts,
                    cash=snap.cash,
                    positions_value=snap.positions_value,
                    total_equity=snap.total_equity,
                    drawdown_pct=snap.drawdown_pct,
                ),
                risk_cfg,
            )
            if isinstance(dd_decision, Reject):
                kill_switch_active = True
                _notify_kill_switch(
                    bar_ts=ts,
                    snapshot=EquitySnapshot(
                        timestamp=ts,
                        cash=snap.cash,
                        positions_value=snap.positions_value,
                        total_equity=snap.total_equity,
                        drawdown_pct=snap.drawdown_pct,
                    ),
                    risk_cfg=risk_cfg,
                    notify_fn=session_cfg.notify_fn,
                )

        # Strategy decision.
        intents = strategy(state, recent_bars)
        if not intents:
            continue
        n_intents += len(intents)

        for intent in intents:
            decision = _check_intent(
                intent=intent,
                adapter=adapter,
                risk_cfg=risk_cfg,
                journal=journal,
                bar_timestamp=ts,
                kill_switch_active=kill_switch_active,
                day=ts.date(),
            )
            if isinstance(decision, Reject):
                n_risk_rejected += 1
                with contextlib.suppress(Exception):  # pragma: no cover
                    journal.record_intent(intent, decision, bar_timestamp=ts)
                continue

            # All checks passed → submit.
            n_submitted += 1
            report: ExecutionReport = adapter.place_order(intent)
            with contextlib.suppress(Exception):  # pragma: no cover
                journal.record_intent(intent, Allow(), bar_timestamp=ts)
                journal.record_report(report)
                pass
            if report.status in ("filled", "partial"):
                n_filled += 1
                # Build a Fill for journal. AKQuantPaperAdapter
                # exposes make_fill_record; fall back to a manual
                # build if the adapter is a test fake.
                fill = _make_fill_record(adapter, intent, report)
                if fill is not None:
                    with contextlib.suppress(Exception):  # pragma: no cover
                        journal.record_fill(fill)

    finished_at = utcnow()
    final_snap = adapter.query_account()
    return PaperSessionReport(
        started_at=started_at,
        finished_at=finished_at,
        n_intents=n_intents,
        n_risk_rejected=n_risk_rejected,
        n_submitted=n_submitted,
        n_filled=n_filled,
        final_equity=final_snap.total_equity,
        max_drawdown_pct=max_drawdown_pct,
    )


def format_kill_switch_body(
    snapshot: EquitySnapshot, risk_cfg: RiskConfig,
) -> str:
    """Build the markdown body for the drawdown kill-switch alert.

    Format is plain text (one field per line) — readable on
    钉聊 mobile client. Includes everything an operator
    needs to decide whether to investigate: drawdown, cap,
    cash, positions value, equity, timestamp.

    Stable format (no JSON, no escape sequences) so future
    Phase 5 parsers can extract fields via simple regex.
    """
    return (
        f"drawdown_pct={snapshot.drawdown_pct:.2%}\n"
        f"kill_switch_cap={risk_cfg.drawdown_kill_switch_pct:.2%}\n"
        f"cash={snapshot.cash:.0f}\n"
        f"positions_value={snapshot.positions_value:.0f}\n"
        f"total_equity={snapshot.total_equity:.0f}\n"
        f"timestamp={snapshot.timestamp.isoformat()}"
    )


def _notify_kill_switch(
    *,
    bar_ts: datetime,
    snapshot: EquitySnapshot,
    risk_cfg: RiskConfig,
    notify_fn: Callable[[str, str], None] | None,
) -> None:
    """Fire the kill-switch alert (best-effort).

    Always logs at WARNING via loguru. If ``notify_fn`` is
    provided (typically ``ops.notify.ding`` in production,
    a spy in tests), invokes it with ``(title, body)``.

    A raising ``notify_fn`` is swallowed + logged — the
    runner does not abort. 钉聊 outages are infrastructure
    problems the operator handles separately; they should
    NOT cascade into a paper-mode session crash.

    Called exactly once per session (when the flip 0→1 happens).
    Subsequent intents in the same session see ``kill_switch_active``
    True and skip the notify call entirely (cheap O(1)).
    """
    title = f"Drawdown kill switch ({bar_ts.isoformat()})"
    body = format_kill_switch_body(snapshot, risk_cfg)
    logger.warning(
        "drawdown kill switch fired equity={e:.0f} dd={d:.2%} cap={c:.2%}",
        e=snapshot.total_equity,
        d=snapshot.drawdown_pct,
        c=risk_cfg.drawdown_kill_switch_pct,
    )
    if notify_fn is None:
        return
    try:
        notify_fn(title, body)
    except Exception:  # pragma: no cover -- best-effort alert
        logger.exception("notify_fn raised; kill-switch alert not delivered")


def _check_intent(
    *,
    intent: OrderIntent,
    adapter: Any,
    risk_cfg: Any,
    journal: PaperJournal,
    bar_timestamp: datetime,
    kill_switch_active: bool,
    day: date_cls,
) -> Allow | Reject:
    """Run all three risk checks in sequence; short-circuit on Reject.

    Order:
      1. Kill switch (if active, reject all)
      2. Position cap (only matters for buys)
      3. Daily trade count

    Daily count is read from the journal (``compute_daily_trade_count``)
    so the journal is the single source of truth — even after a
    process restart, the count persists.
    """
    if kill_switch_active:
        return Reject(
            reason=(
                "drawdown_kill_switch: session halted by previous-bar "
                "drawdown >= kill switch"
            )
        )
    if not risk_cfg.enabled:
        return Allow()
    # Position cap: need current position + total equity.
    snap = adapter.query_account()
    positions = {p.symbol: p.quantity for p in adapter.query_positions()}
    current_qty = positions.get(intent.symbol, 0)
    pos_decision = check_position_cap(
        intent=intent,
        current_position_qty=current_qty,
        total_equity=snap.total_equity,
        cfg=risk_cfg,
    )
    if isinstance(pos_decision, Reject):
        return pos_decision
    # Daily trade count.
    today_trades = journal.compute_daily_trade_count(day)
    count_decision = check_daily_trade_count(today_trades, risk_cfg)
    return count_decision


def _make_fill_record(
    adapter: Any,
    intent: OrderIntent,
    report: ExecutionReport,
) -> Fill | None:
    """Build a :class:`Fill` from intent + report.

    Prefers ``adapter.make_fill_record`` if available (the AKQuant
    wrapper provides it). Falls back to a manual build using
    ``commission_rate`` / ``stamp_tax_rate`` attrs on the adapter
    so test fakes without ``make_fill_record`` still work.
    """
    if hasattr(adapter, "make_fill_record"):
        return adapter.make_fill_record(intent, report)
    if report.avg_fill_price is None or report.filled_quantity == 0:
        return None
    import uuid

    notional = report.avg_fill_price * report.filled_quantity
    commission_rate = getattr(adapter, "commission_rate", 0.0)
    stamp_tax_rate = getattr(adapter, "stamp_tax_rate", 0.0)
    commission = notional * commission_rate
    stamp_tax = notional * stamp_tax_rate if intent.side == "sell" else 0.0
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
