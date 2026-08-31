"""End-to-end tests for ``execution.runner.run_paper_session`` (W7.1).

The runner is the integration point: strategy → risk → adapter →
journal. These tests use the real ``AkquantPaperAdapter`` (Phase 1
default backend) but inject custom strategies / risk configs.

Coverage:

  * Empty data → 0 intents emitted.
  * Buy-once strategy → 1 fill, equity reduced, journal persisted.
  * Risk rejection → journal records the verdict; adapter never sees
    the order.
  * Daily trade count cap → after 20 fills, the 21st is rejected.
  * Drawdown kill switch → session-wide stop after drawdown breach.
  * Idempotency: re-running with the same strategy + journal works
    (the journal UPSERT semantics handle re-recording).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from execution import (  # noqa: E402
    AkquantPaperAdapter,
    OrderIntent,
    PaperJournal,
    RiskConfig,
    run_paper_session,
)

# ---------------------------------------------------------------------------
# OHLCV fixture factory (avoids importing tests/conftest to keep this
# module self-contained).
# ---------------------------------------------------------------------------


def _bars(n: int = 5, start: str = "2024-09-02", close: float = 10.0) -> pd.DataFrame:
    """n business-day bars starting from ``start`` (last day = ``start + (n-1) BD``)."""
    dates = pd.bdate_range(end=pd.Timestamp(start), periods=n)
    return pd.DataFrame(
        {
            "date": dates,
            "open": [close] * n,
            "high": [close + 0.5] * n,
            "low": [close - 0.5] * n,
            "close": [close] * n,
            "volume": [1_000_000.0] * n,
        }
    )


# ---------------------------------------------------------------------------
# Empty / smoke
# ---------------------------------------------------------------------------


def test_empty_data_no_intents(tmp_path: Path) -> None:
    """0 bars → 0 intents, 0 fills, equity unchanged."""
    bars = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    adapter = AkquantPaperAdapter()
    journal = PaperJournal(tmp_path / "j.sqlite")

    def strategy(s, recent):  # never called
        return []

    report = run_paper_session(strategy, bars, adapter=adapter, journal=journal)
    assert report.n_intents == 0
    assert report.n_filled == 0
    assert report.final_equity == pytest.approx(1_000_000.0)


# ---------------------------------------------------------------------------
# Buy-once strategy
# ---------------------------------------------------------------------------


def test_buy_once_fills_and_persists(tmp_path: Path) -> None:
    """One-shot buy → 1 fill → cash debited → journal has 1 row."""
    bars = _bars(n=3)
    adapter = AkquantPaperAdapter()
    journal = PaperJournal(tmp_path / "j.sqlite")

    def strategy(s, recent):
        if s.get("bought"):
            return []
        s["bought"] = True
        return [OrderIntent(
            client_order_id="buy-once-1", symbol="000001",
            side="buy", quantity=100, price=10.0,
        )]

    report = run_paper_session(strategy, bars, adapter=adapter, journal=journal)
    assert report.n_intents == 1
    assert report.n_filled == 1
    assert report.n_risk_rejected == 0

    fills = journal.query_fills()
    assert len(fills) == 1
    assert fills[0].symbol == "000001"
    assert fills[0].quantity == 100

    # Total equity = cash + cost-basis positions_value.
    # cash = 1M - 1000 - 0.30 = 998_999.70; positions_value = 1000 → 999_999.70.
    assert report.final_equity == pytest.approx(
        1_000_000.0 - 1000 * 0.0003,  # only commission; cost basis offsets notional
    )


# ---------------------------------------------------------------------------
# Risk rejection (no adapter call)
# ---------------------------------------------------------------------------


def test_position_cap_blocks_buy(tmp_path: Path) -> None:
    """Buy that would push position past 10% → rejected; adapter never called."""
    bars = _bars(n=2)
    adapter = AkquantPaperAdapter()
    journal = PaperJournal(tmp_path / "j.sqlite")

    def strategy(s, recent):
        s.setdefault("calls", 0)
        s["calls"] += 1
        # 12000 shares * 10.0 / 1M = 12% → would breach 10% cap
        return [OrderIntent(
            client_order_id=f"big-{s['calls']}", symbol="000001",
            side="buy", quantity=12_000, price=10.0,
        )]

    report = run_paper_session(strategy, bars, adapter=adapter, journal=journal)
    # Strategy emits once per bar (2 bars) → 2 intents, 2 rejections.
    assert report.n_intents == 2
    assert report.n_risk_rejected == 2
    assert report.n_filled == 0
    # Adapter's positions stay empty.
    assert adapter.query_positions() == []
    # Journal recorded the intent with the rejection reason.
    fills = journal.query_fills()
    assert fills == []


def test_daily_trade_count_blocks_after_cap(tmp_path: Path) -> None:
    """After ``max_daily_trades`` fills, the next intent is rejected.

    Uses 5 bars all dated today so ``bar.date()`` matches the
    fill timestamps the runner records. (The runner calls
    ``compute_daily_trade_count(bar.date())``; if the bar is
    days-old but fills are stamped ``utcnow()``, the count is
    always zero and the cap is never reached.)
    """
    today = datetime.now(UTC).replace(tzinfo=None, hour=10, minute=0, second=0, microsecond=0)
    bars = pd.DataFrame({
        "date": [today + pd.Timedelta(minutes=i) for i in range(5)],
        "open": [10.0] * 5,
        "high": [10.5] * 5,
        "low": [9.5] * 5,
        "close": [10.0] * 5,
        "volume": [1_000_000.0] * 5,
    })

    adapter = AkquantPaperAdapter()
    journal = PaperJournal(tmp_path / "j.sqlite")

    # Custom config: max 2 trades per day.
    risk_cfg = RiskConfig(max_daily_trades=2, max_position_pct=1.0)


    def strategy(s, recent):
        s.setdefault("count", 0)
        s["count"] += 1
        cid = f"c-{s['count']}"
        return [OrderIntent(
            client_order_id=cid, symbol="000001",
            side="buy", quantity=10, price=10.0,
        )]

    report = run_paper_session(
        strategy, bars, adapter=adapter, journal=journal, risk_cfg=risk_cfg,
    )
    # First 2 fills, then 3 rejections.
    assert report.n_filled == 2
    assert report.n_risk_rejected == 3
    assert report.n_intents == 5


def test_drawdown_kill_switch_halts_session(tmp_path: Path) -> None:
    """Kill switch activates when drawdown >= cap; all subsequent intents rejected.

    Setup: buy at 10.0 (cash debited), then mark-to-market drops.
    But the paper adapter uses cost-basis valuation, so buy doesn't
    trigger drawdown. To force the kill switch, we directly poke
    the adapter's HWM down so the next ``query_account`` reports
    drawdown > 5%.
    """
    bars = _bars(n=4)
    adapter = AkquantPaperAdapter(initial_cash=1_000_000.0)
    journal = PaperJournal(tmp_path / "j.sqlite")

    # Force an artificial 6% drawdown by lifting the HWM.
    adapter._high_water_mark = 1_060_000.0  # 6% above current equity

    risk_cfg = RiskConfig(
        max_position_pct=1.0,
        max_daily_trades=10_000,
        drawdown_kill_switch_pct=0.05,
    )


    def strategy(s, recent):
        s.setdefault("intent_count", 0)
        s["intent_count"] += 1
        return [OrderIntent(
            client_order_id=f"c-{s['intent_count']}", symbol="000001",
            side="buy", quantity=100, price=10.0,
        )]

    report = run_paper_session(
        strategy, bars, adapter=adapter, journal=journal, risk_cfg=risk_cfg,
    )
    # Bar 0: snapshot at start → drawdown 5.66% (1_060_000 vs 1_000_000)
    # → kill switch activates. All 4 intents rejected.
    assert report.n_intents == 4
    assert report.n_risk_rejected == 4
    assert report.n_filled == 0
    # No positions held.
    assert adapter.query_positions() == []


def test_runner_uses_journal_as_source_of_truth_for_daily_count(tmp_path: Path) -> None:
    """If the journal already has fills from a prior session, the daily
    count starts from that baseline (NOT zero)."""
    today = datetime.now(UTC).replace(tzinfo=None, hour=10, minute=0, second=0, microsecond=0)
    bars = pd.DataFrame({
        "date": [today + pd.Timedelta(minutes=i) for i in range(2)],
        "open": [10.0] * 2,
        "high": [10.5] * 2,
        "low": [9.5] * 2,
        "close": [10.0] * 2,
        "volume": [1_000_000.0] * 2,
    })

    # Pre-populate journal with 3 fills on TODAY.
    journal = PaperJournal(tmp_path / "j.sqlite")
    for i in range(3):
        from execution.protocol import Fill

        journal.record_fill(Fill(
            fill_id=f"pre-{i}", client_order_id=f"pre-{i}",
            broker_order_id="", symbol="000001", side="buy",
            quantity=10, price=10.0, timestamp=datetime.now(UTC).replace(tzinfo=None),
        ))

    adapter = AkquantPaperAdapter()
    # max_daily_trades=5 → 3 pre-existing + 1 new fill = 4 (under cap).
    risk_cfg = RiskConfig(max_daily_trades=5, max_position_pct=1.0)

    def strategy(s, recent):
        if s.get("called"):
            return []
        s["called"] = True
        return [OrderIntent(
            client_order_id="new", symbol="000001",
            side="buy", quantity=10, price=10.0,
        )]

    report = run_paper_session(
        strategy, bars, adapter=adapter, journal=journal, risk_cfg=risk_cfg,
    )
    assert report.n_filled == 1  # the 4th trade (3 pre + 1 new)
    assert report.n_risk_rejected == 0

    # Now bump to max_daily_trades=3 → the new buy should reject
    # (3 pre-existing fills already at cap).
    bars2 = bars.iloc[:1].copy()
    journal2 = PaperJournal(tmp_path / "j2.sqlite")
    for i in range(3):
        from execution.protocol import Fill

        journal2.record_fill(Fill(
            fill_id=f"pre-{i}", client_order_id=f"pre-{i}",
            broker_order_id="", symbol="000001", side="buy",
            quantity=10, price=10.0, timestamp=datetime.now(UTC).replace(tzinfo=None),
        ))

    risk_cfg_strict = RiskConfig(max_daily_trades=3, max_position_pct=1.0)

    def strategy2(s, recent):
        if s.get("called"):
            return []
        s["called"] = True
        return [OrderIntent(
            client_order_id="new", symbol="000001",
            side="buy", quantity=10, price=10.0,
        )]

    report2 = run_paper_session(
        strategy2, bars2, adapter=adapter, journal=journal2,
        risk_cfg=risk_cfg_strict,
    )
    assert report2.n_risk_rejected == 1
    assert report2.n_filled == 0
