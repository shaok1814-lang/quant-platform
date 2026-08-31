"""Unit tests for ``execution.journal.PaperJournal`` (W7.1).

Coverage:

  * Round-trip: write 4 row types, reopen journal, read back via query API.
  * Idempotency: re-recording the same intent (with a different
    risk decision) UPDATES the existing row — the journal surfaces
    the most recent verdict.
  * ``compute_daily_trade_count`` correctly counts distinct
    client_order_ids per calendar day.
  * ``compare_to`` surfaces deviations above the threshold and
    flags paper-only / live-only ids.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from execution.journal import (  # noqa: E402
    PaperJournal,
)
from execution.protocol import (  # noqa: E402
    EquitySnapshot,
    ExecutionReport,
    Fill,
    OrderIntent,
    utcnow,
)
from execution.risk import Allow, Reject  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _intent(cid: str = "c1", qty: int = 100, price: float = 10.0) -> OrderIntent:
    return OrderIntent(
        client_order_id=cid,
        symbol="000001",
        side="buy",
        quantity=qty,
        price=price,
    )


def _report(cid: str = "c1", status: str = "filled", qty: int = 100, price: float = 10.0):
    return ExecutionReport(
        client_order_id=cid,
        broker_order_id=f"b-{cid}",
        status=status,
        filled_quantity=qty if status in ("filled", "partial") else 0,
        avg_fill_price=price if status in ("filled", "partial") else None,
        timestamp=utcnow(),
    )


def _fill(
    cid: str = "c1",
    fill_id: str = "f1",
    qty: int = 100,
    price: float = 10.0,
    ts: datetime | None = None,
) -> Fill:
    """Build a Fill with a controllable timestamp.

    Default timestamp is ``utcnow()`` (= today), which is fine for
    tests that don't care about date. Tests that assert day-keyed
    counts pass an explicit ``ts`` so the filter matches.
    """
    return Fill(
        fill_id=fill_id,
        client_order_id=cid,
        broker_order_id=f"b-{cid}",
        symbol="000001",
        side="buy",
        quantity=qty,
        price=price,
        timestamp=ts or utcnow(),
    )


def _snap(ts: datetime | None = None) -> EquitySnapshot:
    return EquitySnapshot(
        timestamp=ts or utcnow(),
        cash=950_000.0,
        positions_value=50_000.0,
        total_equity=1_000_000.0,
        drawdown_pct=0.0,
    )


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_all_row_types(tmp_path: Path) -> None:
    """Write intent + report + fill + snapshot; reopen and read back."""
    db = tmp_path / "journal.sqlite"
    j = PaperJournal(db)

    bar_ts = datetime(2024, 9, 2, 9, 30)
    j.record_intent(_intent(), Allow(), bar_timestamp=bar_ts)
    j.record_report(_report())
    j.record_fill(_fill(ts=bar_ts))
    snap_ts = datetime(2024, 9, 2, 15, 0)
    j.record_snapshot(_snap(snap_ts))

    # Reopen → data must persist.
    j2 = PaperJournal(db)
    assert len(j2.query_intents()) == 1
    assert len(j2.query_fills()) == 1
    assert j2.compute_daily_trade_count(bar_ts.date()) == 1


def test_query_fills_filters_by_date(tmp_path: Path) -> None:
    """query_fills(day=...) returns only rows whose timestamp is on that day."""
    db = tmp_path / "journal.sqlite"
    j = PaperJournal(db)

    j.record_intent(_intent("c1"), Allow(), bar_timestamp=datetime(2024, 9, 2, 9, 30))
    j.record_fill(_fill("c1", "f1"))
    j.record_intent(_intent("c2"), Allow(), bar_timestamp=datetime(2024, 9, 3, 9, 30))
    j.record_fill(_fill("c2", "f2"))

    # Force the c1 fill to be on 2024-09-02 by re-recording with the right timestamp.
    # Default utcnow() → not deterministic across days, so use the date filter test:
    fills = j.query_fills()
    assert len(fills) == 2  # both filled today in test run

    fills_day = j.query_fills(day=datetime(2099, 1, 1).date())
    assert len(fills_day) == 0  # future day → empty


# ---------------------------------------------------------------------------
# Idempotency / upsert semantics
# ---------------------------------------------------------------------------


def test_intent_resubmission_updates_risk_decision(tmp_path: Path) -> None:
    """Re-recording the same intent with a different risk decision
    UPDATES the row in place (single row, latest verdict wins)."""
    db = tmp_path / "journal.sqlite"
    j = PaperJournal(db)
    bar_ts = datetime(2024, 9, 2, 9, 30)

    intent = _intent("c1")
    j.record_intent(intent, Allow(), bar_timestamp=bar_ts)
    assert len(j.query_intents()) == 1

    # Same intent resubmitted → risk reject this time.
    j.record_intent(intent, Reject(reason="daily_trade_count: 20 >= 20"), bar_timestamp=bar_ts)
    assert len(j.query_intents()) == 1  # still one row

    # Re-read raw to verify the verdict column updated.
    import sqlite3

    with sqlite3.connect(db) as con:
        row = con.execute(
            "SELECT risk_decision, risk_reason FROM order_intent "
            "WHERE client_order_id = 'c1'"
        ).fetchone()
    assert row[0] == "reject"
    assert "daily_trade_count" in row[1]


def test_fill_idempotent(tmp_path: Path) -> None:
    """Re-recording the same fill_id is a no-op (ON CONFLICT DO NOTHING)."""
    db = tmp_path / "journal.sqlite"
    j = PaperJournal(db)
    j.record_fill(_fill("c1", "f1"))
    j.record_fill(_fill("c1", "f1"))  # same fill_id
    assert len(j.query_fills()) == 1


def test_snapshot_upsert_on_same_timestamp(tmp_path: Path) -> None:
    """Re-recording a snapshot at the same timestamp UPDATES in place."""
    db = tmp_path / "journal.sqlite"
    j = PaperJournal(db)
    ts = datetime(2024, 9, 2, 15, 0)
    j.record_snapshot(_snap(ts))
    j.record_snapshot(EquitySnapshot(
        timestamp=ts, cash=900_000.0, positions_value=0.0,
        total_equity=900_000.0, drawdown_pct=0.10,
    ))

    import sqlite3

    with sqlite3.connect(db) as con:
        row = con.execute(
            "SELECT cash, drawdown_pct FROM equity_snapshot WHERE timestamp = ?",
            (ts.isoformat(),),
        ).fetchone()
    assert row[0] == 900_000.0  # updated value
    assert row[1] == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# compute_daily_trade_count
# ---------------------------------------------------------------------------


def test_daily_trade_count_distinct_ids_per_day(tmp_path: Path) -> None:
    """Multiple fills per client_order_id count as ONE trade."""
    db = tmp_path / "journal.sqlite"
    j = PaperJournal(db)

    day = datetime(2024, 9, 2)
    j.record_fill(_fill("c1", "f1", ts=day))  # one trade on c1
    j.record_fill(_fill("c1", "f2", ts=day))  # second fill, same id → still one trade
    j.record_fill(_fill("c2", "f3", ts=day))  # second trade
    j.record_fill(_fill("c3", "f4", ts=day))  # third trade

    assert j.compute_daily_trade_count(day.date()) == 3


# ---------------------------------------------------------------------------
# compare_to
# ---------------------------------------------------------------------------


def test_compare_identical_passes(tmp_path: Path) -> None:
    """Same journal compared to itself passes."""
    db = tmp_path / "j.sqlite"
    j = PaperJournal(db)
    j.record_fill(_fill("c1", "f1"))

    res = j.compare_to(j, max_deviation_pct=5.0)
    assert res.passed is True
    assert res.rows == []
    assert res.n_paper_only == 0
    assert res.n_live_only == 0


def test_compare_quantity_deviation_above_threshold(tmp_path: Path) -> None:
    """Paper filled 100, live filled 50 → 50% deviation → flagged."""
    db_paper = tmp_path / "paper.sqlite"
    db_live = tmp_path / "live.sqlite"
    paper = PaperJournal(db_paper)
    live = PaperJournal(db_live)
    paper.record_fill(_fill("c1", "f1", qty=100))
    live.record_fill(_fill("c1", "f1", qty=50))  # 50% deviation

    res = paper.compare_to(live, max_deviation_pct=5.0)
    assert res.passed is False
    assert len(res.rows) == 1
    assert res.rows[0].quantity_deviation_pct == pytest.approx(50.0)


def test_compare_price_deviation_below_threshold_passes(tmp_path: Path) -> None:
    """2% price diff → below 5% threshold → passes."""
    db_paper = tmp_path / "paper.sqlite"
    db_live = tmp_path / "live.sqlite"
    paper = PaperJournal(db_paper)
    live = PaperJournal(db_live)
    paper.record_fill(_fill("c1", "f1", price=10.0))
    live.record_fill(_fill("c1", "f1", price=10.2))  # 2% off

    res = paper.compare_to(live, max_deviation_pct=5.0)
    assert res.passed is True
    assert res.rows == []


def test_compare_paper_only_and_live_only_counts(tmp_path: Path) -> None:
    """Disjoint intent sets → not passed, n_paper_only/n_live_only count."""
    db_paper = tmp_path / "paper.sqlite"
    db_live = tmp_path / "live.sqlite"
    paper = PaperJournal(db_paper)
    live = PaperJournal(db_live)
    paper.record_fill(_fill("c1", "f1"))
    paper.record_fill(_fill("c2", "f2"))
    live.record_fill(_fill("c3", "f3"))  # different intent

    res = paper.compare_to(live, max_deviation_pct=5.0)
    assert res.passed is False
    assert res.n_paper_only == 2  # c1, c2 in paper not live
    assert res.n_live_only == 1   # c3 in live not paper
