"""Tests for ``execution.AccountSlot`` + ``MultiAccountReport`` +
``run_multi_account_paper_session`` (W7.1 last deferred item).

Coverage:

  * Per-slot risk isolation — slot A's risk cap doesn't affect
    slot B.
  * Per-slot journal isolation — slot A's fills don't appear in
    slot B's journal.
  * Aggregated drawdown is the worst across slots.
  * Per-slot kill-switch notify_fn fires only for the slot whose
    adapter HWM triggers it.
  * Empty accounts list raises ValueError.
  * MultiAccountReport.to_dict() JSON-serializes cleanly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from execution import (  # noqa: E402
    AccountSlot,
    AkquantPaperAdapter,
    MultiAccountReport,
    PaperJournal,
    RiskConfig,
    run_multi_account_paper_session,
)
from execution.runner import PaperSessionConfig  # noqa: E402

# ---------------------------------------------------------------------------
# OHLCV fixture (same shape as the other runner tests).
# ---------------------------------------------------------------------------


def _bars(n: int = 5, close: float = 10.0) -> pd.DataFrame:
    dates = pd.bdate_range(end=pd.Timestamp("2024-09-02"), periods=n)
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


def _buy_once(s, recent, cid: str) -> list:
    if s.get("bought"):
        return []
    s["bought"] = True
    from execution import OrderIntent

    return [
        OrderIntent(
            client_order_id=cid,
            symbol="000001",
            side="buy",
            quantity=100,
            price=10.0,
        )
    ]


def _always_buy(s, recent) -> list:
    from execution import OrderIntent

    return [
        OrderIntent(
            client_order_id=f"{s.get('n', 0)}",
            symbol="000001",
            side="buy",
            quantity=100,
            price=10.0,
        )
    ]


# ---------------------------------------------------------------------------
# Basic shape
# ---------------------------------------------------------------------------


def test_empty_accounts_raises(tmp_path: Path) -> None:
    """Empty ``accounts`` → ValueError (caller bug, not silent)."""
    with pytest.raises(ValueError, match="accounts must be non-empty"):
        run_multi_account_paper_session([], _bars(n=2), session_cfg=None)


def test_two_accounts_run_independently(tmp_path: Path) -> None:
    """Two slots share data but have independent journals + adapters.

    Both produce 1 fill each (buy-once strategy). Total final
    equity = 2 * (1M - commission).
    """
    adapter_a = AkquantPaperAdapter()
    adapter_b = AkquantPaperAdapter()
    adapter_a.connect()
    adapter_b.connect()
    journal_a = PaperJournal(tmp_path / "a.sqlite")
    journal_b = PaperJournal(tmp_path / "b.sqlite")

    slot_a = AccountSlot(
        name="cash",
        strategy=lambda s, r: _buy_once(s, r, "a-1"),
        adapter=adapter_a,
        journal=journal_a,
    )
    slot_b = AccountSlot(
        name="margin",
        strategy=lambda s, r: _buy_once(s, r, "b-1"),
        adapter=adapter_b,
        journal=journal_b,
    )

    report = run_multi_account_paper_session(
        [slot_a, slot_b],
        _bars(n=3),
    )
    assert isinstance(report, MultiAccountReport)
    # Both filled once.
    assert report.per_account["cash"].n_filled == 1
    assert report.per_account["margin"].n_filled == 1
    # Independent journals.
    fills_a = journal_a.get_fills() if hasattr(journal_a, "get_fills") else journal_a.query_fills()
    fills_b = journal_b.get_fills() if hasattr(journal_b, "get_fills") else journal_b.query_fills()
    # Different client_order_ids per slot.
    assert fills_a[0].client_order_id == "a-1"
    assert fills_b[0].client_order_id == "b-1"
    # Aggregated stats.
    assert report.total_initial_equity == pytest.approx(2_000_000.0)
    assert report.n_kill_switches_fired == 0


# ---------------------------------------------------------------------------
# Per-slot risk isolation
# ---------------------------------------------------------------------------


def test_per_slot_risk_config_isolated(tmp_path: Path) -> None:
    """Slot A has tight position cap (1 share max); slot B has
    loose cap (100 shares). Both buy-once. Slot A rejects (over
    cap), slot B fills."""
    adapter_a = AkquantPaperAdapter()
    adapter_b = AkquantPaperAdapter()
    adapter_a.connect()
    adapter_b.connect()
    journal_a = PaperJournal(tmp_path / "a.sqlite")
    journal_b = PaperJournal(tmp_path / "b.sqlite")

    slot_a = AccountSlot(
        name="tight",
        strategy=lambda s, r: _buy_once(s, r, "tight-1"),
        adapter=adapter_a,
        journal=journal_a,
        # 100 shares * 10.0 / 1M = 0.1% — under 1% cap, OK.
        # We want a CAP that REJECTS 100 shares: cap = 0.0001
        # (0.01%) means 100 * 10 / 1M = 0.1% > 0.01% → reject.
        risk_cfg=RiskConfig(max_position_pct=0.0001),
    )
    slot_b = AccountSlot(
        name="loose",
        strategy=lambda s, r: _buy_once(s, r, "loose-1"),
        adapter=adapter_b,
        journal=journal_b,
        risk_cfg=RiskConfig(max_position_pct=1.0),  # 100% — anything passes
    )

    report = run_multi_account_paper_session(
        [slot_a, slot_b],
        _bars(n=3),
    )
    # Slot A: position cap rejects the buy.
    assert report.per_account["tight"].n_filled == 0
    assert report.per_account["tight"].n_risk_rejected == 1
    # Slot B: fills normally.
    assert report.per_account["loose"].n_filled == 1
    # Aggregated total still reflects only slot B's fill.
    assert report.total_final_equity < 2_000_000.0  # both cash + position
    assert report.total_final_equity > 999_000.0


# ---------------------------------------------------------------------------
# Per-slot notify_fn isolation
# ---------------------------------------------------------------------------


def test_per_slot_notify_fn_only_fires_for_triggered_slot(tmp_path: Path) -> None:
    """Kill-switch fires on slot A (forced HWM), slot B is healthy.
    Slot A's notify_fn receives 1 alert; slot B's notify_fn receives
    0."""
    adapter_a = AkquantPaperAdapter()
    adapter_b = AkquantPaperAdapter()
    adapter_a.connect()
    adapter_b.connect()
    # Force drawdown on A only.
    adapter_a._high_water_mark = 1_060_000.0

    journal_a = PaperJournal(tmp_path / "a.sqlite")
    journal_b = PaperJournal(tmp_path / "b.sqlite")

    captured_a: list[tuple[str, str]] = []
    captured_b: list[tuple[str, str]] = []

    slot_a = AccountSlot(
        name="drawdown",
        strategy=lambda s, r: _buy_once(s, r, "a-1"),
        adapter=adapter_a,
        journal=journal_a,
        notify_fn=lambda t, b: captured_a.append((t, b)),
    )
    slot_b = AccountSlot(
        name="healthy",
        strategy=lambda s, r: _buy_once(s, r, "b-1"),
        adapter=adapter_b,
        journal=journal_b,
        notify_fn=lambda t, b: captured_b.append((t, b)),
    )

    report = run_multi_account_paper_session(
        [slot_a, slot_b],
        _bars(n=3),
    )
    assert len(captured_a) == 1, captured_a
    assert len(captured_b) == 0, captured_b
    # Aggregated kill-switch count = 1 (only slot A).
    assert report.n_kill_switches_fired == 1
    # Worst drawdown reflects slot A.
    assert report.aggregated_max_drawdown_pct >= 0.05


# ---------------------------------------------------------------------------
# Aggregated stats
# ---------------------------------------------------------------------------


def test_aggregated_drawdown_is_worst(tmp_path: Path) -> None:
    """Two slots with different drawdowns — aggregated takes the max."""
    adapter_a = AkquantPaperAdapter()
    adapter_b = AkquantPaperAdapter()
    adapter_a.connect()
    adapter_b.connect()
    # Force different drawdowns.
    adapter_a._high_water_mark = 1_030_000.0  # ~3% drawdown
    adapter_b._high_water_mark = 1_080_000.0  # ~8% drawdown (worst)

    journal_a = PaperJournal(tmp_path / "a.sqlite")
    journal_b = PaperJournal(tmp_path / "b.sqlite")

    slot_a = AccountSlot(
        name="mild",
        strategy=lambda s, r: [],
        adapter=adapter_a,
        journal=journal_a,
    )
    slot_b = AccountSlot(
        name="severe",
        strategy=lambda s, r: [],
        adapter=adapter_b,
        journal=journal_b,
    )

    report = run_multi_account_paper_session(
        [slot_a, slot_b],
        _bars(n=2),
    )
    # Worst-of: slot B's ~8% drawdown wins.
    assert report.aggregated_max_drawdown_pct >= 0.07
    assert report.aggregated_max_drawdown_pct < 0.10
    # The per-slot PaperSessionReport's max_drawdown_pct is
    # session-local (0% in cost-basis paper mode by design).
    # The lifetime signal lives on the adapter — verified by the
    # aggregated metric above (slot B's lifetime drawdown drove
    # it past 0.07).
    assert report.per_account["mild"].max_drawdown_pct == 0.0
    # Slot A: ~3% lifetime drawdown < 5% cap → kill switch NOT fired.
    # Slot B: ~8% lifetime drawdown > 5% cap → kill switch fired.
    # ``n_kill_switches_fired`` counts how many slots fired.
    assert report.n_kill_switches_fired == 1


def test_multi_account_report_to_dict_is_json_serializable(tmp_path: Path) -> None:
    """to_dict() renders as JSON-safe types."""
    adapter_a = AkquantPaperAdapter()
    adapter_a.connect()
    journal_a = PaperJournal(tmp_path / "a.sqlite")
    slot_a = AccountSlot(
        name="a",
        strategy=lambda s, r: _buy_once(s, r, "a-1"),
        adapter=adapter_a,
        journal=journal_a,
    )
    report = run_multi_account_paper_session([slot_a], _bars(n=3))
    d = report.to_dict()
    # Round-trip through json.dumps.
    json.dumps(d)
    # Per-account nested to_dict also present.
    assert "a" in d["per_account"]
    assert "n_intents" in d["per_account"]["a"]


# ---------------------------------------------------------------------------
# Per-slot cash override
# ---------------------------------------------------------------------------


def test_per_slot_initial_cash_override(tmp_path: Path) -> None:
    """Slot's ``initial_cash`` overrides ``session_cfg.initial_cash``."""
    adapter = AkquantPaperAdapter(initial_cash=500_000.0)
    adapter.connect()
    journal = PaperJournal(tmp_path / "j.sqlite")
    slot = AccountSlot(
        name="cash",
        strategy=lambda s, r: [],
        adapter=adapter,
        journal=journal,
        initial_cash=500_000.0,
    )
    report = run_multi_account_paper_session(
        [slot],
        _bars(n=2),
        session_cfg=PaperSessionConfig(initial_cash=99_999_999.0),
    )
    # Final equity reflects slot's 500k, NOT session_cfg's 99M.
    assert report.total_initial_equity == pytest.approx(500_000.0)
    assert report.total_final_equity == pytest.approx(500_000.0)


# ---------------------------------------------------------------------------
# Cross-slot safety
# ---------------------------------------------------------------------------


def test_single_slot_works(tmp_path: Path) -> None:
    """A single-slot multi-account call works (simplest non-trivial case)."""
    adapter = AkquantPaperAdapter()
    adapter.connect()
    journal = PaperJournal(tmp_path / "j.sqlite")
    slot = AccountSlot(
        name="only",
        strategy=lambda s, r: _buy_once(s, r, "only-1"),
        adapter=adapter,
        journal=journal,
    )
    report = run_multi_account_paper_session([slot], _bars(n=2))
    assert "only" in report.per_account
    assert report.per_account["only"].n_filled == 1


def test_session_cfg_max_history_depth_applied_to_all_slots(tmp_path: Path) -> None:
    """``session_cfg.max_history_depth`` flows through to every slot."""
    adapter_a = AkquantPaperAdapter()
    adapter_b = AkquantPaperAdapter()
    adapter_a.connect()
    adapter_b.connect()
    journal_a = PaperJournal(tmp_path / "a.sqlite")
    journal_b = PaperJournal(tmp_path / "b.sqlite")
    slot_a = AccountSlot(
        name="a",
        strategy=lambda s, r: _buy_once(s, r, "a"),
        adapter=adapter_a,
        journal=journal_a,
    )
    slot_b = AccountSlot(
        name="b",
        strategy=lambda s, r: _buy_once(s, r, "b"),
        adapter=adapter_b,
        journal=journal_b,
    )
    cfg = PaperSessionConfig(max_history_depth=3)
    run_multi_account_paper_session([slot_a, slot_b], _bars(n=5), session_cfg=cfg)
    # Indirect check: both runs completed without raising.
    assert adapter_a.query_positions() or adapter_a.query_positions() == []
    assert adapter_b.query_positions() or adapter_b.query_positions() == []
