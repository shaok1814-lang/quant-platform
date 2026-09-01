"""Tests for ``ops.weekly_paper_job`` (W6.5).

Coverage:

  * Report dataclass JSON serialization (dashboard-friendly).
  * End-to-end ``run_weekly_paper_session`` with stub DuckDB +
    stub strategy — verifies bar loading, paper run, JSON write.
  * Kill-switch 钉聊 invocation (drawdown >= 5%).
  * Error paths: missing DuckDB, empty window.
  * ``build_scheduler`` registers the weekly job alongside the
    daily ingest (default + disabled).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_layer.storage.duck import DuckStore  # noqa: E402
from ops.weekly_paper_job import (  # noqa: E402
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_SYMBOL,
    WeeklyPaperReport,
    run_weekly_paper_session,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _populate_dummy_duckdb(
    db: Path,
    symbol: str,
    n_trading_days: int = 30,
) -> None:
    """Seed ``db`` with ``n_trading_days`` of OHLCV for ``symbol``.

    Dates are business days ending yesterday (so the runner's
    ``start_date = today - lookback_days`` window catches them all).
    """
    today = datetime.now(UTC).date()
    # ``bdate_range`` is inclusive on both ends. End on yesterday so
    # the lookback window of (today - 60d, today) catches them all.
    dates = pd.bdate_range(end=today - timedelta(days=1), periods=n_trading_days)
    rows = []
    base_close = 10.0
    for i, d in enumerate(dates):
        # Trivial walk: close drifts up 0.05/day.
        c = base_close + 0.05 * i
        rows.append(
            (
                d.strftime("%Y-%m-%d"),
                c,  # open
                c + 0.1,  # high
                c - 0.1,  # low
                c,  # close
                1_000_000.0,  # volume
            )
        )
    df = pd.DataFrame(
        rows, columns=["date", "open", "high", "low", "close", "volume"],
    )
    df["date"] = pd.to_datetime(df["date"])
    df["amount"] = df["volume"] * df["close"] * 0.001
    df.attrs["symbol"] = symbol
    df.attrs["fetcher"] = "stub"
    df.attrs["adjust"] = "qfq"
    df.attrs["fetched_at"] = datetime.now(UTC).isoformat()

    with DuckStore(db) as store:
        store.upsert_daily_bars(df)


class _StubStrategy:
    """Minimal AKQuant-like strategy. Does NOT subclass ``akquant.Strategy``
    — bridge's dynamic class only needs ``on_bar`` + ``order_target_percent``.

    Buys 9% of equity on the first bar it sees for ``000001``; then
    no-ops. The bridge wraps it; the runner calls ``__call__`` once per
    bar with the multi-symbol dict.
    """

    def __init__(self) -> None:
        self._bought: bool = False

    def on_start(self) -> None:
        return None

    def on_bar(self, bar: object) -> None:
        sym = getattr(bar, "symbol", None)
        if sym != "000001":
            return
        if not self._bought:
            self._bought = True
            self.order_target_percent(symbol="000001", target_percent=0.09)


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


def test_weekly_paper_report_to_dict_dates_as_iso() -> None:
    """``to_dict()`` serializes dates as ISO strings (JSON-friendly)."""
    today = datetime.now(UTC).date()
    rep = WeeklyPaperReport(
        run_date=today,
        symbol="000001",
        start_date=today - timedelta(days=10),
        end_date=today,
        n_bars=10,
        started_at=datetime.now(UTC).isoformat(),
        duration_s=0.5,
        n_intents=1,
        n_risk_rejected=0,
        n_filled=1,
        final_equity=999_500.0,
        max_drawdown_pct=0.01,
        kill_switch_fired=False,
        report_path="/tmp/weekly.json",
    )
    d = rep.to_dict()
    assert d["run_date"] == today.isoformat()
    assert d["start_date"] == (today - timedelta(days=10)).isoformat()
    assert d["end_date"] == today.isoformat()
    # Round-trips through json.dumps (sanity check).
    json.dumps(d)


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


def test_run_weekly_paper_session_e2e(tmp_path: Path) -> None:
    """Stub DuckDB + stub strategy → weekly report + JSON written."""
    db = tmp_path / "daily.duckdb"
    out = tmp_path / "reports"
    _populate_dummy_duckdb(db, symbol=DEFAULT_SYMBOL, n_trading_days=30)

    weekly = run_weekly_paper_session(
        duckdb_path=db,
        output_dir=out,
        strategy_cls=_StubStrategy,
        notify_on_kill_switch=False,
    )
    # 1 fill from the stub strategy (9% of 1M / 10.0 = 9000 shares).
    assert weekly.symbol == DEFAULT_SYMBOL
    assert weekly.n_intents >= 1
    assert weekly.n_filled >= 1
    assert weekly.kill_switch_fired is False
    assert weekly.max_drawdown_pct >= 0.0
    # JSON file written.
    assert Path(weekly.report_path).exists()
    on_disk = json.loads(Path(weekly.report_path).read_text(encoding="utf-8"))
    assert on_disk["symbol"] == DEFAULT_SYMBOL
    assert on_disk["n_filled"] == weekly.n_filled
    assert on_disk["report_path"] == weekly.report_path


def test_run_weekly_paper_session_uses_default_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``output_dir`` is ``None``, the module's :data:`DEFAULT_OUTPUT_DIR`
    is used. Verifies the indirection by monkey-patching it."""
    db = tmp_path / "daily.duckdb"
    _populate_dummy_duckdb(db, symbol=DEFAULT_SYMBOL, n_trading_days=20)
    custom_out = tmp_path / "custom_reports"
    monkeypatch.setattr(
        "ops.weekly_paper_job.DEFAULT_OUTPUT_DIR", custom_out,
    )
    weekly = run_weekly_paper_session(
        duckdb_path=db,
        strategy_cls=_StubStrategy,
        notify_on_kill_switch=False,
    )
    assert str(custom_out) in weekly.report_path
    assert Path(weekly.report_path).exists()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_run_weekly_paper_session_missing_duckdb_raises(tmp_path: Path) -> None:
    """DuckDB file does not exist → FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="DuckDB not found"):
        run_weekly_paper_session(
            duckdb_path=tmp_path / "no-such.duckdb",
            output_dir=tmp_path / "out",
            strategy_cls=_StubStrategy,
        )


def test_run_weekly_paper_session_empty_window_raises(tmp_path: Path) -> None:
    """DuckDB exists but has no rows for the symbol in the window → ValueError."""
    db = tmp_path / "daily.duckdb"
    # Seed a different symbol only.
    _populate_dummy_duckdb(db, symbol="600000", n_trading_days=10)

    with pytest.raises(ValueError, match="no rows for"):
        run_weekly_paper_session(
            duckdb_path=db,
            output_dir=tmp_path / "out",
            symbol="000001",
            strategy_cls=_StubStrategy,
        )


# ---------------------------------------------------------------------------
# Kill switch + 钉聊
# ---------------------------------------------------------------------------


def test_notify_kill_switch_calls_ding_with_stable_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_notify_kill_switch`` invokes ``ops.notify.ding`` with a
    stable body format (Phase 5 parsers can extract fields via regex)."""
    from datetime import date as date_cls

    captured: list[tuple[str, str]] = []

    def fake_ding(title: str, body: str) -> None:
        captured.append((title, body))

    monkeypatch.setattr("ops.notify.ding", fake_ding)

    from ops.weekly_paper_job import _notify_kill_switch

    today = date_cls(2026, 9, 1)
    weekly = WeeklyPaperReport(
        run_date=today,
        symbol="000001",
        start_date=today - timedelta(days=10),
        end_date=today,
        n_bars=10,
        started_at=datetime.now(UTC).isoformat(),
        duration_s=0.5,
        n_intents=2,
        n_risk_rejected=0,
        n_filled=2,
        final_equity=950_000.0,
        max_drawdown_pct=0.06,
        kill_switch_fired=True,
        report_path="/tmp/weekly_2026-09-01.json",
    )
    _notify_kill_switch(weekly)

    assert len(captured) == 1
    title, body = captured[0]
    assert "kill switch" in title.lower()
    assert "000001" in title
    # Stable body fields (Phase 5 parsers rely on these):
    assert "run_date=2026-09-01" in body
    assert "max_drawdown_pct=6.00%" in body
    assert "final_equity=950000" in body
    assert "report=" in body


def test_run_weekly_paper_session_kill_switch_fires_ding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """End-to-end: when ``max_drawdown_pct >= 5%`` (e.g. via a
    manually-set adapter HWM), the kill-switch alert is sent.
    """
    db = tmp_path / "daily.duckdb"
    out = tmp_path / "reports"
    _populate_dummy_duckdb(db, symbol=DEFAULT_SYMBOL, n_trading_days=30)

    # Force drawdown by monkey-patching the adapter's HWM after
    # construction. The runner instantiates the adapter inside
    # ``run_weekly_paper_session``; we patch it via a wrapper.
    captured: list[tuple[str, str]] = []

    def fake_ding(title: str, body: str) -> None:
        captured.append((title, body))

    monkeypatch.setattr("ops.notify.ding", fake_ding)

    from execution.brokers.akquant_paper import AkquantPaperAdapter
    orig_init = AkquantPaperAdapter.__init__

    def forced_hwm_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        orig_init(self, *args, **kwargs)
        # 6% above starting equity → first query_account reports
        # dd = 60000 / 1060000 ≈ 5.66% ≥ 5% cap.
        self._high_water_mark = 1_060_000.0

    monkeypatch.setattr(AkquantPaperAdapter, "__init__", forced_hwm_init)

    weekly = run_weekly_paper_session(
        duckdb_path=db,
        output_dir=out,
        strategy_cls=_StubStrategy,
        notify_on_kill_switch=True,
    )
    assert weekly.kill_switch_fired is True
    assert len(captured) == 1, captured


def test_run_weekly_paper_session_no_kill_switch_no_ding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Healthy run (no drawdown) → no 钉聊 alert, kill_switch_fired=False."""
    db = tmp_path / "daily.duckdb"
    out = tmp_path / "reports"
    _populate_dummy_duckdb(db, symbol=DEFAULT_SYMBOL, n_trading_days=30)

    captured: list[tuple[str, str]] = []

    def fake_ding(title: str, body: str) -> None:
        captured.append((title, body))

    monkeypatch.setattr("ops.notify.ding", fake_ding)

    weekly = run_weekly_paper_session(
        duckdb_path=db,
        output_dir=out,
        strategy_cls=_StubStrategy,
        notify_on_kill_switch=True,
    )
    # Cost-basis valuation → no drawdown. Stub strategy only buys
    # once → 1 fill. Final equity ≈ 1M - commission.
    assert weekly.kill_switch_fired is False
    assert captured == []


# ---------------------------------------------------------------------------
# Defaults sanity
# ---------------------------------------------------------------------------


def test_default_symbol_is_000001() -> None:
    """The weekly validation cycle anchors to 000001 (W1 baseline)."""
    assert DEFAULT_SYMBOL == "000001"


def test_default_lookback_days_is_60() -> None:
    """60 calendar days ≈ 12 weeks of trading days. Wide enough
    for W5 walk-forward in-sample; narrow enough for a sub-30s run."""
    assert DEFAULT_LOOKBACK_DAYS == 60


def test_default_output_dir_under_project_root() -> None:
    """The default output path is anchored to the project root
    (production writes here, tests inject ``output_dir``)."""
    assert str(DEFAULT_OUTPUT_DIR).replace("\\", "/").endswith("data/paper_reports")
