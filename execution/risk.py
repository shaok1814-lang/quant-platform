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

from backtest.a_share._types import Board
from backtest.a_share.price_limits import (
    is_limit_down,
    is_limit_up,
)

from execution.protocol import (
    EquitySnapshot,
    OrderIntent,
    RiskConfig,
)

__all__ = [
    "Allow",
    "Reject",
    "REASON_DAILY_TRADES",
    "REASON_DISABLED",
    "REASON_DRAWDOWN_KILL",
    "REASON_POSITION_CAP",
    "REASON_PRICE_LIMIT_DOWN",
    "REASON_PRICE_LIMIT_UP",
    "REASON_SUSPENDED",
    "RiskDecision",
    "check_daily_trade_count",
    "check_drawdown_kill_switch",
    "check_position_cap",
    "check_price_limit",
    "check_suspension",
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
REASON_PRICE_LIMIT_UP: Final[str] = "price_limit_up"
REASON_PRICE_LIMIT_DOWN: Final[str] = "price_limit_down"
REASON_SUSPENDED: Final[str] = "suspended"


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
                f"{REASON_DAILY_TRADES}: today_trades {today_trades} >= cap {cfg.max_daily_trades}"
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


# ---------------------------------------------------------------------------
# A-share 涨跌停 (CLAUDE.md 「涨停日不可买入，跌停日不可卖出」)
# ---------------------------------------------------------------------------


def check_price_limit(
    intent: OrderIntent,
    current_close: float,
    prev_close: float,
    *,
    board: Board,
    is_st: bool,
    cfg: RiskConfig,
) -> RiskDecision:
    """Reject buys on 涨停 and sells on 跌停.

    Reuses the canonical ``backtest.a_share.price_limits`` predicates
    so the rule matches exactly what the public docs claim
    (``docs/site/a-share-rules.md``). ST symbols always use the
    5% band regardless of board (CLAUDE.md: ST bands tighter).

    Args:
        intent: The :class:`OrderIntent` to check.
        current_close: Today's close (or the limit reference price
            at the bar the strategy is reacting to).
        prev_close: Yesterday's close (the limit reference base).
        board: One of ``"main"``, ``"chinext"``, ``"star"``, ``"bjs"``.
        is_st: Whether the symbol is currently ST.
        cfg: Active :class:`RiskConfig`. The guard is enabled iff
            ``cfg.enable_price_limit_guard`` is ``True``.

    Returns:
        :class:`Reject` with ``price_limit_up`` / ``price_limit_down``
        reason tag. :class:`Allow` otherwise (including when
        guard is disabled).

    Boundary:
        Same as the pure predicate: a close *exactly* on the
        limit (after rounding) is treated as at-the-limit (reject).
    """
    if not cfg.enable_price_limit_guard:
        return Allow()
    if prev_close <= 0:
        # Defensive: garbage input → don't block the intent.
        # The runner will still check position cap + daily trades.
        return Allow()
    if intent.side == "buy" and is_limit_up(
        current_close, prev_close, is_st=is_st, board=board
    ):
        return Reject(
            reason=(
                f"{REASON_PRICE_LIMIT_UP}: close {current_close} at limit "
                f"(prev_close {prev_close}, board={board}, is_st={is_st}); buy blocked"
            )
        )
    if intent.side == "sell" and is_limit_down(
        current_close, prev_close, is_st=is_st, board=board
    ):
        return Reject(
            reason=(
                f"{REASON_PRICE_LIMIT_DOWN}: close {current_close} at limit "
                f"(prev_close {prev_close}, board={board}, is_st={is_st}); sell blocked"
            )
        )
    return Allow()


# ---------------------------------------------------------------------------
# A-share 停牌 (CLAUDE.md 「停牌日无成交」)
# ---------------------------------------------------------------------------


def check_suspension(
    intent: OrderIntent,
    current_volume: int,
    *,
    cfg: RiskConfig,
) -> RiskDecision:
    """Reject all orders on a bar with ``volume == 0`` (suspected suspension).

    The OHLCV heuristic in ``backtest.a_share.suspension`` is the
    authoritative detector (uses 2-bar flat-stretch in addition to
    zero-volume). This guard implements the volume-zero branch only
    so the runner can short-circuit at the intent layer without
    recomputing the bar-level inference.

    Args:
        intent: The :class:`OrderIntent` to check.
        current_volume: Today's bar volume (integer-share count).
        cfg: Active :class:`RiskConfig`. The guard is enabled iff
            ``cfg.enable_suspension_guard`` is ``True``.

    Returns:
        :class:`Reject` with ``suspended`` reason tag if
        ``current_volume <= 0``. :class:`Allow` otherwise.
    """
    if not cfg.enable_suspension_guard:
        return Allow()
    if current_volume <= 0:
        return Reject(
            reason=(
                f"{REASON_SUSPENDED}: volume {current_volume} <= 0 on the bar; "
                f"intent {intent.side} {intent.quantity or '?'} {intent.symbol} blocked"
            )
        )
    return Allow()
