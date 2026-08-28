"""W4 self-research A-share rules patch layer.

This package ships the **A-share boundary list** that ``CLAUDE.md``
mandates every strategy must explicitly handle. AKQuant ships only
``ChinaStockConfig(enforce_tick_size=True)`` (a closed dataclass with
one field) so every other rule is layered here as a pure-function
utility, with optional opt-in helpers for strategy authors.

Rule coverage matrix:

| Rule | Module | AKQuant also? |
|---|---|---|
| T+1 交割 | (AKQuant ``t_plus_one=True``) | yes |
| 涨跌停 | ``price_limits`` | no |
| 停牌 | ``suspension`` | no |
| 除权除息 (qfq) | ``ex_dividend`` | data-layer qfq (W2) |
| ST 股票过滤 | ``st_filter`` | no |
| 100 股整手 | ``lot_enforcement`` | yes (buy-side strict; ``close_position`` bypasses) |
| 印花税卖单边 | ``stamp_tax`` | yes |
| 幸存者偏差 | ``delisted_universe`` | no |

Architecture: pure-function library. W4 does NOT modify AKQuant and
does NOT modify W3 strategies. Strategies opt in by importing the
utilities they need and (optionally) instantiating an
:class:`AShareRuleChecklist` in ``on_start`` to self-attest that
each rule has been handled (per CLAUDE.md "must be explicitly
handled").

See :file:`./README.md` for the per-rule cookbook.
"""

from __future__ import annotations

from typing import NamedTuple

from backtest.a_share._types import (
    DEFAULT_LOT_SIZE,
    DEFAULT_ST_LIMIT_PCT,
    DEFAULT_STAMP_TAX_RATE,
    Board,
    LimitBounds,
)
from backtest.a_share.delisted_universe import (
    OFFLINE_DELISTED_CSV,
    build_universe,
    fetch_delisted_symbols,
)
from backtest.a_share.ex_dividend import detect_ex_dividend_days
from backtest.a_share.lot_enforcement import enforce_lot, is_valid_lot
from backtest.a_share.price_limits import (
    LIMIT_PCT_BY_BOARD,
    ST_LIMIT_PCT,
    compute_limit_price,
    is_at_limit,
    is_limit_down,
    is_limit_up,
)
from backtest.a_share.st_filter import OFFLINE_ST_CSV, fetch_st_symbols, filter_st
from backtest.a_share.stamp_tax import Side, compute_stamp_tax
from backtest.a_share.suspension import infer_suspension_from_ohlcv


class AShareRuleChecklist(NamedTuple):
    """Strategy author self-attestation for the A-share boundary list.

    Per CLAUDE.md "每次写新策略前,列出涉及到的规则清单", new strategies
    should fill this in ``on_start`` to declare which rules they
    handle. This is **advisory** (no auto-enforcement) but documented
    in ``backtest/a_share/README.md`` for reviewer visibility.

    Example::

        class MyStrategy(akquant.Strategy):
            def on_start(self):
                self._checklist = AShareRuleChecklist(
                    price_limits_checked=True,
                    suspension_checked=True,
                    ex_dividend_checked=True,
                    st_filter_applied=True,
                    delisted_universe_used=True,
                    lot_enforced=True,
                    stamp_tax_acknowledged=True,  # sell-only, AKQuant built-in
                )
    """

    price_limits_checked: bool
    suspension_checked: bool
    ex_dividend_checked: bool
    st_filter_applied: bool
    delisted_universe_used: bool
    lot_enforced: bool
    stamp_tax_acknowledged: bool


__all__ = [
    "DEFAULT_LOT_SIZE",
    "DEFAULT_STAMP_TAX_RATE",
    "DEFAULT_ST_LIMIT_PCT",
    "LIMIT_PCT_BY_BOARD",
    "OFFLINE_DELISTED_CSV",
    "OFFLINE_ST_CSV",
    "ST_LIMIT_PCT",
    "AShareRuleChecklist",
    "Board",
    "LimitBounds",
    "Side",
    "build_universe",
    "compute_limit_price",
    "compute_stamp_tax",
    "detect_ex_dividend_days",
    "enforce_lot",
    "fetch_delisted_symbols",
    "fetch_st_symbols",
    "filter_st",
    "infer_suspension_from_ohlcv",
    "is_at_limit",
    "is_limit_down",
    "is_limit_up",
    "is_valid_lot",
]
