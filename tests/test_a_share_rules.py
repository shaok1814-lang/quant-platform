"""A-share rule test skeleton.

P1 W1 (this task):
  * **A-group**: T+1 (AKQuant built-in ``t_plus_one=...``) — real
    assertion, exercised against a 5-row toy OHLCV frame so no network
    is needed.
  * **B-group**: 6 skipped tests covering the full A-share boundary list
    prescribed by ``CLAUDE.md``: 涨跌停、停牌、除权、ST、退市、最小单位
    严格校验、印花税卖出单边.

P1 W4 replaces B-group skips with real assertions once the self-research
A-share patch layer lands.

Lot-size strict enforcement and sell-only stamp tax are wired in
``research/strategies/ma_cross.py`` via ``run_backtest`` kwargs (AKQuant
built-in is a black box from the test side). P1 W4 will add direct
``trades_df.commission`` / ``trades_df.quantity``-based assertions.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest
from akquant import Strategy, run_backtest

# ---------------------------------------------------------------------------
# A 组：T+1 实证（AKQuant 内置）
# ---------------------------------------------------------------------------


class _BuyThenTrySellStrategy(Strategy):
    """Buy on bar 1, try to flatten on bar 2, then stop acting.

    This isolates the T+1 effect on bar 2 specifically:
      * ``t_plus_one=True``  → bar-2 flatten rejected → 0 closed trades
      * ``t_plus_one=False`` → bar-2 flatten succeeds → 1 closed trade

    We intentionally do NOT keep re-attempting the flatten on bars 3+, or
    T+1 would only delay the sell (it'd still close eventually), and the
    two parametrize cases would produce identical closed-trade counts.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._bars_seen = 0

    def on_start(self) -> None:
        self.set_history_depth(1)

    def on_bar(self, bar):  # type: ignore[no-untyped-def]
        # 0.85 leaves headroom for buy-side commission + transfer fee
        # so the buy fills on a 1M-cash account.
        self._bars_seen += 1
        if self._bars_seen == 1:
            self.order_target_percent(symbol=bar.symbol, target_percent=0.85)
        elif self._bars_seen == 2:
            self.order_target_percent(symbol=bar.symbol, target_percent=0.0)
        # bars 3+ : no action — see class docstring


def _toy_ohlcv(n_bars: int = 5) -> pd.DataFrame:
    """Generate ``n_bars`` strictly-increasing synthetic OHLCV (no network)."""
    dates = pd.bdate_range(end=pd.Timestamp("2024-01-12"), periods=n_bars)
    closes = np.linspace(10.0, 11.0, n_bars)
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": closes + 0.01,
            "low": closes - 0.01,
            "close": closes,
            "volume": np.full(n_bars, 1_000_000.0),
        }
    )


@pytest.mark.parametrize(
    ("t_plus_one", "expect_closed_trades"),
    [
        pytest.param(True, 0, id="t+1=on-same-day-sell-rejected"),
        pytest.param(False, 1, id="t+1=off-same-day-sell-allowed"),
    ],
)
def test_t_plus_one_blocks_same_day_sell(t_plus_one: bool, expect_closed_trades: int) -> None:
    """Verify AKQuant's built-in ``t_plus_one`` actually enforces T+1.

    With 5 daily bars and a buy-on-bar-1 / try-to-sell-on-every-other-bar
    strategy, we count ``BacktestResult.trades_df``:

      * ``t_plus_one=True``  → 0 closed trades (sell rejected by T+1)
      * ``t_plus_one=False`` → 1 closed trade (sell succeeds next day)

    If the count drifts from the expected value, AKQuant's T+1 behaviour
    has changed and P1 W4 / patch-layer work must re-validate.
    """
    df = _toy_ohlcv(5)
    result = run_backtest(
        data=df,
        strategy=_BuyThenTrySellStrategy,
        symbols=["000001"],
        start_time="2024-01-08",
        end_time="2024-01-15",
        initial_cash=1_000_000,
        lot_size=100,
        commission_rate=0.0003,
        stamp_tax_rate=0.001,
        t_plus_one=t_plus_one,
        history_depth=1,
        warmup_period=0,
    )
    trades = result.trades_df
    assert len(trades) == expect_closed_trades, (
        f"t_plus_one={t_plus_one} expected {expect_closed_trades} closed "
        f"trades, got {len(trades)}: {trades.to_dict('records')}"
    )


# ---------------------------------------------------------------------------
# B 组：6 项 stub，等待 P1 W4 A 股自研补丁落地
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="A-share patch layer not implemented yet (P1 W4). "
    "Will assert: on a 涨停 day, buy orders are rejected."
)
def test_limit_up_blocks_buy() -> None: ...


@pytest.mark.skip(
    reason="A-share patch layer not implemented yet (P1 W4). "
    "Will assert: on a 跌停 day, sell orders are rejected."
)
def test_limit_down_blocks_sell() -> None: ...


@pytest.mark.skip(
    reason="A-share patch layer not implemented yet (P1 W4). "
    "Will assert: suspended days produce no fills and are excluded "
    "from position valuation."
)
def test_suspension_no_fill() -> None: ...


@pytest.mark.skip(
    reason="A-share patch layer not implemented yet (P1 W4). "
    "Will assert: close-to-close return on a dividend day matches the "
    "ex-dividend adjustment factor ratio."
)
def test_ex_dividend_adjustment() -> None: ...


@pytest.mark.skip(
    reason="A-share patch layer not implemented yet (P1 W4). "
    "Will assert: ST-flagged symbols are excluded from the universe by "
    "default; explicit opt-in required."
)
def test_st_symbol_filter() -> None: ...


@pytest.mark.skip(
    reason="A-share patch layer not implemented yet (P1 W4). "
    "Will assert: delisted symbols are retained in the backtest "
    "universe to mitigate survivor bias."
)
def test_delisted_symbol_inclusion() -> None: ...
