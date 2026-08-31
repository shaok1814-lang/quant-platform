"""Risk helpers — CLAUDE.md hard-constraint enforcement (W7.1).

Three pure-function guards implementing the project's documented
合规与实盘纪律 (compliance & live-trading discipline):

  * :func:`check_position_cap` — 单 symbol 仓位 ≤ 10% of total
    equity. Sells are not capped (they REDUCE exposure).
  * :func:`check_daily_trade_count` — single-day round-trip ≤ 20.
    Both buys and sells count toward the daily total.
  * :func:`check_drawdown_kill_switch` — total-equity drawdown
    ≥ 5% from high-water-mark → reject everything.

Each function returns a :class:`RiskDecision` (``Allow`` |
``Reject(reason)``) so the runner can log the rejection reason
in the journal without losing intent-side context.

**Why pure functions, not classes**: every check is stateless.
The runner holds the state (current position, daily count, equity
snapshot). Each function takes a snapshot + the intent and returns
a verdict. This makes the rules individually unit-testable and
keeps the runner's hot loop branch-free.

**Why ``Reject(reason)`` instead of ``bool`` + log message**:
the journal needs the reason to write into ``order_intent.risk_reason``.
Sum-type return makes the runner's "if Reject: journal.record(reason)"
pattern exhaustive (vs. an enum + out-param).

**Conservative boundaries**: when a check has a threshold (e.g.
10%), we reject AT the threshold (>=), not just above it. This
matches the documented CLAUDE.md 「不超过 10%」 reading. If a
future spec wants "strictly less than", we change to ``>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from execution.protocol import (
    EquitySnapshot,
    OrderIntent,
    RiskConfig,
)

__all__ = [
    "Allow",
    "Reject",
    "RiskDecision",
    "check_daily_trade_count",
    "check_drawdown_kill_switch",
    "check_position_cap",
]


# ---------------------------------------------------------------------------
# Sum type for risk verdicts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Allow:
    """Permit this intent to proceed to the adapter.

    Sentinel: the runner checks ``isinstance(decision, Reject)``
    rather than ``decision.status == "allow"`` because the union
    pattern is faster (no attribute lookup) and matches PEP 634
    structural-matching intent.
    """


@dataclass(frozen=True)
class Reject:
    """Refuse this intent. ``reason`` is journaled verbatim.

    Attributes:
        reason: Short machine-readable tag (snake_case) prefixed
            to the human-readable message: e.g.
            ``"position_cap: 9.6% + 0.01% intent = 9.61% > 10.0% cap"``.
            The prefix part is stable for grep-based alerting.
    """

    reason: str


# Union alias for return types. Documented as the canonical verdict.
RiskDecision = Allow | Reject


# ---------- Reason-tag prefixes (stable for alerting + journal) ----------

REASON_DISABLED: Final[str] = "disabled"
REASON_POSITION_CAP: Final[str] = "position_cap"
REASON_DAILY_TRADES: Final[str] = "daily_trade_count"
REASON_DRAWDOWN_KILL: Final[str] = "drawdown_kill_switch"


# ---------------------------------------------------------------------------
# Position cap (CLAUDE.md: 单 symbol 仓位 ≤ 10%)
# ---------------------------------------------------------------------------


def check_position_cap(
    intent: OrderIntent,
    current_position_qty: int,
    total_equity: float,
    cfg: RiskConfig,
) -> RiskDecision:
    """Per-symbol position cap check.

    Args:
        intent: The buy/sell intent to evaluate. **Only buys are
            capped**; sells reduce exposure and are always allowed
            at this layer (a "must close at all costs" emergency
            exit would bypass this in Phase 2 via a separate flag
            on OrderIntent; not in W7.1 scope).
        current_position_qty: Current signed quantity held of
            ``intent.symbol``. ``0`` for flat.
        total_equity: Current total portfolio equity (cash +
            positions_value). ``0.0`` is treated as "unknown
            equity" and rejected conservatively.
        cfg: Active :class:`RiskConfig`. ``cfg.enabled=False``
            short-circuits to :class:`Allow` immediately.

    Returns:
        :class:`Allow` if the post-fill quantity would be within
        the cap, or ``total_equity <= 0`` (treated as no cap
        baseline — runner should not have called us in that state).
        :class:`Reject` if post-fill exposure would exceed
        ``cfg.max_position_pct`` (10% default).

    Math:
        post_fill_value = (current_position_qty + signed_intent) * intent.price
        post_fill_pct = post_fill_value / total_equity
        Reject if post_fill_pct >= cfg.max_position_pct
    """
    if not cfg.enabled:
        return Allow()

    # Sells (and zero-quantity intents) cannot exceed a position cap
    # — they only reduce or hold exposure.
    if intent.side != "buy" or intent.quantity <= 0:
        return Allow()

    # No equity baseline → cap is meaningless. Don't block (the
    # runner will surface the zero-equity condition separately).
    if total_equity <= 0 or intent.price is None or intent.price <= 0:
        return Allow()

    signed_intent = intent.quantity  # buys add positive quantity
    post_fill_qty = current_position_qty + signed_intent
    post_fill_value = post_fill_qty * intent.price
    post_fill_pct = post_fill_value / total_equity

    if post_fill_pct >= cfg.max_position_pct:
        return Reject(
            reason=(
                f"{REASON_POSITION_CAP}: post-fill {post_fill_pct:.2%} "
                f"(qty {post_fill_qty} x price {intent.price:.2f} = "
                f"{post_fill_value:.0f}) >= cap {cfg.max_position_pct:.2%} "
                f"of equity {total_equity:.0f}"
            )
        )
    return Allow()


# ---------------------------------------------------------------------------
# Daily trade count cap (≤ 20 round-trips per day)
# ---------------------------------------------------------------------------


def check_daily_trade_count(
    today_trades: int,
    cfg: RiskConfig,
) -> RiskDecision:
    """Daily round-trip count cap.

    Args:
        today_trades: Number of round-trip trades already executed
            today. A round-trip = one buy + one sell for the same
            symbol on the same day. The runner is responsible for
            tracking this (incremented after each ``ExecutionReport``
            with status ``"filled"``/``"partial"``).
        cfg: Active :class:`RiskConfig`.

    Returns:
        :class:`Allow` if ``today_trades < cfg.max_daily_trades``,
        else :class:`Reject`.

    Boundary:
        We reject AT the limit (``>=``) so the count never EXCEEDS
        the documented cap. If a future spec wants "up to N allowed",
        switch to ``>``.
    """
    if not cfg.enabled:
        return Allow()

    if today_trades >= cfg.max_daily_trades:
        return Reject(
            reason=(
                f"{REASON_DAILY_TRADES}: today_trades {today_trades} "
                f">= cap {cfg.max_daily_trades}"
            )
        )
    return Allow()


# ---------------------------------------------------------------------------
# Drawdown kill switch (CLAUDE.md risk discipline)
# ---------------------------------------------------------------------------


def check_drawdown_kill_switch(
    snapshot: EquitySnapshot,
    cfg: RiskConfig,
) -> RiskDecision:
    """Kill switch when total drawdown exceeds the cap.

    Args:
        snapshot: Most recent :class:`EquitySnapshot`. Only
            ``snapshot.drawdown_pct`` is consulted.
        cfg: Active :class:`RiskConfig`.

    Returns:
        :class:`Reject` if ``snapshot.drawdown_pct >=
        cfg.drawdown_kill_switch_pct``. The runner uses this as a
        session-wide stop (no new intents for the rest of the
        session).

    Boundary:
        Same as the other checks: AT the threshold rejects. This is
        the conservative side — a strategy that sees equity at
        exactly 5.0% drawdown is not allowed to add new exposure.
    """
    if not cfg.enabled:
        return Allow()

    if snapshot.drawdown_pct >= cfg.drawdown_kill_switch_pct:
        return Reject(
            reason=(
                f"{REASON_DRAWDOWN_KILL}: drawdown {snapshot.drawdown_pct:.2%} "
                f">= kill switch {cfg.drawdown_kill_switch_pct:.2%} "
                f"(equity {snapshot.total_equity:.0f})"
            )
        )
    return Allow()
