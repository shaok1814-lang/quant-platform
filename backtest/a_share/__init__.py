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
