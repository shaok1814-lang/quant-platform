"""Slim project-owned shapes mirroring xtquant SDK objects.

The real xtquant SDK ships ~30 attributes per object
(``XtOrder``, ``XtTrade``, etc.). The runner + adapter only ever
need a handful. Keeping project-owned dataclasses lets us:

  * Build test fixtures without xtquant installed
  * Avoid leaking ``xtquant.xttrader.XtOrder`` types across the
    ``execution/`` public surface
  * Pin the attribute set the adapter depends on (a real xtquant
    release with renamed fields breaks the wrapper, not downstream
    consumers)

**Naming**: ``Xt*`` (capital X, lowercase t) to echo the SDK's
``XtOrder`` convention. The leading ``Fake`` (in ``xtquant_fake``)
and ``My`` (in tests) prefixes are reserved for the test doubles.

**All dataclasses are frozen**: callers MUST build new instances
on every state change (the SDK does this internally; we surface
the same pattern).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "XT_ORDER_STATUS_CANCELLED",
    "XT_ORDER_STATUS_FILLED",
    "XT_ORDER_STATUS_PARTIALLY_FILLED",
    "XT_ORDER_STATUS_REJECTED",
    "XT_ORDER_STATUS_SUBMITTED",
    "XT_ORDER_STATUS_UNKNOWN",
    "XtAsset",
    "XtOrder",
    "XtPosition",
    "XtTrade",
]


# ---------------------------------------------------------------------------
# Order status constants (mirrors xtquant.xtconstant; duplicated here so
# tests can construct XtOrder objects without importing xtquant).
# ---------------------------------------------------------------------------

XT_ORDER_STATUS_UNREPORTED: Final[int] = 0  # 未报
XT_ORDER_STATUS_WAIT_REPORTING: Final[int] = 1  # 待报
XT_ORDER_STATUS_REPORTED: Final[int] = 2  # 已报
#: Alias for the initial state right after we call ``order_stock``
#: (the SDK moves the snapshot through UNREPORTED → WAIT_REPORTING →
#: REPORTED on its own; our adapter treats the whole pre-FILLED
#: range as "submitted").
XT_ORDER_STATUS_SUBMITTED: Final[int] = 2  # 已报 (alias)
XT_ORDER_STATUS_PARTIALLY_FILLED: Final[int] = 3  # 部成
XT_ORDER_STATUS_FILLED: Final[int] = 53  # 全成
XT_ORDER_STATUS_REJECTED: Final[int] = 54  # 废单
XT_ORDER_STATUS_CANCELLED: Final[int] = 55  # 已撤
XT_ORDER_STATUS_UNKNOWN: Final[int] = -1  # 未知


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class XtOrder:
    """Minimal :class:`XtOrder` shape consumed by the adapter.

    Attributes mirror the SDK's fields the wrapper reads. Unused
    SDK fields (strategy_name, cancel_time, error_id, etc.) are
    omitted — adding them later is backwards-compatible.
    """

    order_id: int
    order_remark: str
    stock_code: str
    """Full ``"<6-digit>.SH"`` / ``"<6-digit>.SZ"`` symbol. Adapter
    strips the suffix before routing to OrderIntent."""
    order_type: int
    """xtconstant.STOCK_BUY (23) or STOCK_SELL (24)."""
    price_type: int
    """xtconstant.FIX_PRICE (11) or MARKET_PRICE (12)."""
    order_volume: int
    price: float
    traded_volume: int = 0
    traded_price: float = 0.0
    order_status: int = XT_ORDER_STATUS_UNREPORTED
    order_time: str = ""
    """ISO-8601 string from the SDK; we keep the string rather than
    datetime to avoid tzinfo wrangling. ``""`` for tests."""


@dataclass(frozen=True)
class XtTrade:
    """Minimal :class:`XtTrade` shape.

    One order can produce multiple XtTrade events (partial fills).
    Each event fires ``on_trade`` exactly once.
    """

    order_id: int
    stock_code: str
    direction: int
    """xtconstant.STOCK_BUY / STOCK_SELL — kept as int so callers
    can map without re-importing xtconstant."""
    traded_volume: int
    traded_price: float
    traded_amount: float
    """``traded_volume * traded_price`` snapshot — kept as a
    precomputed field for journal writes (avoid re-multiplying
    on every record)."""
    traded_time: str = ""


@dataclass(frozen=True)
class XtPosition:
    """Minimal :class:`XtPosition` shape.

    SDK has both ``volume`` (total held) and ``can_use_volume``
    (sellable). Adapter only needs ``volume``; ``can_use_volume``
    is exposed for journal queries.
    """

    stock_code: str
    volume: int
    can_use_volume: int
    avg_price: float
    market_value: float = 0.0


@dataclass(frozen=True)
class XtAsset:
    """Minimal :class:`XtAsset` shape.

    The SDK distinguishes ``cash`` / ``frozen_cash`` / ``available_cash``
    / ``market_value`` / ``total_asset``. The adapter reports the
    union via our :class:`EquitySnapshot`; we keep the same field
    names here so the translation is mechanical.
    """

    account_id: str
    cash: float
    frozen_cash: float
    market_value: float
    total_asset: float
    available_cash: float = 0.0
