"""Tests for ``ops.ingest_job.run_daily_ingest`` (W6.1.4).

The pipeline composes universe / fetcher / quality / DuckStore /
notify. Tests stub each layer independently so:

  * No akshare network call is made.
  * No real DuckDB file is touched.
  * No 钉聊 webhook fires (we disable ``notify_on_hard``).
  * Each test owns its own tiny ``universe.yaml`` in ``tmp_path``
    so the suite can run in parallel without state leak.

Coverage:

  * Happy path: stub fetcher returns a clean df for both symbols
    → both upserted, DuckDB row count matches input.
  * Per-symbol isolation: symbol A ok, symbol B fetcher_errors
    → A upserted, B reported as ``fetcher_error``, run_daily_ingest
    never raises.
  * HARD quality failure: stub returns df with NaN close → symbol
    marked ``skipped_hard_quality``, DuckDB NOT touched for that
    symbol (verified via per-symbol row count before / after).
  * SOFT quality (outlier): stub returns df with 25% jump → symbol
    IS upserted (SOFT does not block), but quality report carries
    the SOFT issue for later inspection.
  * Empty df on non-trading day: stub returns empty df → counted
    as 0-row upsert (no quality report, no alert).
  * Idempotency: running twice yields identical DuckDB row count.
"""

from __future__ import annotations

import sys
from datetime import date as date_cls
from pathlib import Path

import duckdb
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_layer.storage.duck import DuckStore  # noqa: E402
from ops.ingest_job import IngestReport, ingest_window, run_daily_ingest  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_universe(tmp_path: Path, symbols: list[tuple[str, str, str]]) -> Path:
    """Write a tiny universe.yaml to ``tmp_path``.

    Args:
        symbols: list of ``(symbol, name, sector)`` tuples.
    """
    lines = ["universe:"]
    for sym, name, sector in symbols:
        lines.append(f"  - {{symbol: '{sym}', name: '{name}', sector: '{sector}'}}")
    p = tmp_path / "universe.yaml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _good_df() -> pd.DataFrame:
    """Synthetic 1-bar df for the test target date.

    Includes the ``amount`` column so ``DuckStore.upsert_daily_bars``
    (which selects on the full ``CORE_COLUMNS`` set) doesn't trip a
    column-missing binder error.
    """
    return pd.DataFrame(
        {
            "date": [pd.Timestamp(date_cls(2024, 9, 2))],
            "open": [10.00],
            "high": [10.50],
            "low": [9.95],
            "close": [10.20],
            "volume": [1_000_000.0],
            "amount": [10_000_000.0],
        }
    )


def _nan_close_df() -> pd.DataFrame:
    """Df that triggers HARD-NaN in close column."""
    df = _good_df()
    df.loc[0, "close"] = float("nan")
    return df


def _outlier_df() -> pd.DataFrame:
    """Df that triggers SOFT-OutlierReturn (|return| > 20%).

    Trick: we provide a 2-bar df so the return check has both a
    prev and curr bar (a 1-bar df cannot trigger returns)."""
    base = _good_df()
    today = base.loc[0].to_dict()
    prev = {**today, "date": pd.Timestamp(date_cls(2024, 9, 1))}
    return pd.DataFrame([prev, today])


def _count_rows(db_path: Path, symbol: str) -> int:
    """Read ``daily_bars`` row count for ``symbol`` from ``db_path``."""
    with duckdb.connect(str(db_path), read_only=True) as con:
        return con.execute("SELECT COUNT(*) FROM daily_bars WHERE symbol = ?", [symbol]).fetchone()[
            0
        ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _make_fetcher(mapper: dict[str, pd.DataFrame | Exception]) -> object:
    """Build a stub fetcher that returns mapped dfs / raises mapped
    exceptions per symbol. Missing keys default to FetcherError
    (simulates an akshare outage for that symbol).

    The returned dfs are annotated with the same ``df.attrs``
    provenance that :func:`data_layer.ingestion.akshare_fetcher
    .fetch_daily_bars` sets, so :meth:`DuckStore.upsert_daily_bars`
    can read ``df.attrs['symbol']`` / ``fetcher`` / ``adjust`` /
    ``fetched_at`` without crashing.
    """
    from datetime import UTC, datetime

    from data_layer.ingestion.akshare_fetcher import FetcherError

    def _fetch(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        if symbol not in mapper:
            raise FetcherError(f"stub: no data for {symbol}")
        item = mapper[symbol]
        if isinstance(item, BaseException):
            raise item
        df = item.copy()
        df.attrs["symbol"] = symbol
        df.attrs["fetcher"] = "stub"
        df.attrs["adjust"] = "qfq"
        df.attrs["fetched_at"] = datetime.now(UTC).isoformat()
        return df

    return _fetch


def test_run_daily_ingest_happy_path(tmp_path: Path) -> None:
    """Clean stub fetcher → all symbols upserted, DuckDB row count
    matches input."""
    db = tmp_path / "test.duckdb"
    universe = _write_universe(tmp_path, [("000001", "A", "x"), ("000002", "B", "y")])
    fetcher = _make_fetcher({"000001": _good_df(), "000002": _good_df()})

    report = run_daily_ingest(
        date=date_cls(2024, 9, 2),
        duckdb_path=db,
        universe_path=universe,
        fetcher=fetcher,
        notify_on_hard=False,
    )

    assert isinstance(report, IngestReport)
    assert report.n_upserted == 2
    assert report.n_hard_quality == 0
    assert report.n_fetcher_errors == 0
    assert _count_rows(db, "000001") == 1
    assert _count_rows(db, "000002") == 1


def test_run_daily_ingest_per_symbol_isolation(tmp_path: Path) -> None:
    """Symbol A ok, symbol B fetcher-error → A upserted, B reported,
    NEVER raised."""
    db = tmp_path / "test.duckdb"
    universe = _write_universe(tmp_path, [("000001", "A", "x"), ("000002", "B", "y")])
    fetcher = _make_fetcher({"000001": _good_df(), "000002": RuntimeError("boom")})

    report = run_daily_ingest(
        date=date_cls(2024, 9, 2),
        duckdb_path=db,
        universe_path=universe,
        fetcher=fetcher,
        notify_on_hard=False,
    )

    assert report.n_upserted == 1
    assert report.n_other_errors == 1  # generic RuntimeError → "other_error"
    # 000001 WAS written; 000002 was NOT.
    assert _count_rows(db, "000001") == 1
    assert _count_rows(db, "000002") == 0
    # The error is recorded on the per-symbol report.
    failed = next(r for r in report.per_symbol if r.symbol == "000002")
    assert failed.status == "other_error"
    assert "boom" in (failed.error or "")


def test_run_daily_ingest_fetcher_error_classified(tmp_path: Path) -> None:
    """``FetcherError`` is reported as ``fetcher_error`` (not
    ``other_error``) so the operator sees a network/data issue
    distinctly from a programming bug."""
    from data_layer.ingestion.akshare_fetcher import FetcherError

    db = tmp_path / "test.duckdb"
    universe = _write_universe(tmp_path, [("000001", "A", "x")])
    fetcher = _make_fetcher({"000001": FetcherError("network down")})

    report = run_daily_ingest(
        date=date_cls(2024, 9, 2),
        duckdb_path=db,
        universe_path=universe,
        fetcher=fetcher,
        notify_on_hard=False,
    )

    assert report.n_fetcher_errors == 1
    assert report.n_other_errors == 0
    assert report.per_symbol[0].status == "fetcher_error"


def test_run_daily_ingest_hard_quality_skips_upsert(tmp_path: Path) -> None:
    """A HARD-quality issue prevents the upsert — DuckDB row count
    for that symbol stays at 0."""
    db = tmp_path / "test.duckdb"
    universe = _write_universe(tmp_path, [("000001", "A", "x")])
    fetcher = _make_fetcher({"000001": _nan_close_df()})

    report = run_daily_ingest(
        date=date_cls(2024, 9, 2),
        duckdb_path=db,
        universe_path=universe,
        fetcher=fetcher,
        notify_on_hard=False,
    )

    assert report.n_hard_quality == 1
    assert report.n_upserted == 0
    assert _count_rows(db, "000001") == 0
    # Quality report carried into the PerSymbolReport for diagnostics.
    rec = report.per_symbol[0]
    assert rec.quality is not None
    assert rec.quality.has_hard_issues
    assert any(i.kind == "NAN_CLOSE" for i in rec.quality.issues)


def test_run_daily_ingest_soft_quality_passes_through(tmp_path: Path) -> None:
    """A SOFT issue (outlier return) does NOT block the upsert;
    rows are written, but the SOFT issue is visible in the report."""
    db = tmp_path / "test.duckdb"
    universe = _write_universe(tmp_path, [("000001", "A", "x")])
    # 25% close-to-close jump: SOFT-OutlierReturn.
    outlier = _outlier_df()
    outlier.loc[1, "close"] = outlier.loc[0, "close"] * 1.25
    outlier.loc[1, "open"] = outlier.loc[1, "close"]
    outlier.loc[1, "high"] = outlier.loc[1, "close"] + 0.05
    outlier.loc[1, "low"] = outlier.loc[1, "close"] - 0.05
    fetcher = _make_fetcher({"000001": outlier})

    report = run_daily_ingest(
        date=date_cls(2024, 9, 2),
        duckdb_path=db,
        universe_path=universe,
        fetcher=fetcher,
        notify_on_hard=False,
    )

    assert report.n_upserted == 1
    assert report.n_hard_quality == 0
    assert _count_rows(db, "000001") == 2  # both bars written
    rec = report.per_symbol[0]
    assert rec.quality is not None
    assert rec.quality.has_soft_issues
    assert not rec.quality.has_hard_issues


def test_run_daily_ingest_empty_df_is_noop(tmp_path: Path) -> None:
    """Non-trading-day stub returns empty df → counted as 0-row
    upsert, no quality report."""
    db = tmp_path / "test.duckdb"
    universe = _write_universe(tmp_path, [("000001", "A", "x")])
    fetcher = _make_fetcher({"000001": pd.DataFrame()})

    report = run_daily_ingest(
        date=date_cls(2024, 9, 2),
        duckdb_path=db,
        universe_path=universe,
        fetcher=fetcher,
        notify_on_hard=False,
    )

    assert report.n_upserted == 1  # empty df still counts as "upserted (0 rows)"
    assert _count_rows(db, "000001") == 0


def test_run_daily_ingest_is_idempotent(tmp_path: Path) -> None:
    """Running twice on the same date does not double-write rows
    (DuckDB upsert overwrites in place)."""
    db = tmp_path / "test.duckdb"
    universe = _write_universe(tmp_path, [("000001", "A", "x")])
    fetcher = _make_fetcher({"000001": _good_df()})

    r1 = run_daily_ingest(
        date=date_cls(2024, 9, 2),
        duckdb_path=db,
        universe_path=universe,
        fetcher=fetcher,
        notify_on_hard=False,
    )
    r2 = run_daily_ingest(
        date=date_cls(2024, 9, 2),
        duckdb_path=db,
        universe_path=universe,
        fetcher=fetcher,
        notify_on_hard=False,
    )

    assert r1.n_upserted == 1
    assert r2.n_upserted == 1
    assert _count_rows(db, "000001") == 1  # NOT 2


def test_run_daily_ingest_with_existing_db_does_not_clobber(tmp_path: Path) -> None:
    """Pre-existing rows in DuckDB (e.g. from a backfill) are kept
    when the new ingest doesn't cover those dates."""
    db = tmp_path / "test.duckdb"
    universe = _write_universe(tmp_path, [("000001", "A", "x")])

    # Pre-populate with a backfilled row on a different date.
    backfill = _good_df()
    backfill["date"] = pd.Timestamp(date_cls(2024, 8, 30))
    backfill.attrs["symbol"] = "000001"
    backfill.attrs["fetcher"] = "stub"
    backfill.attrs["adjust"] = "qfq"
    backfill.attrs["fetched_at"] = "2026-08-29T00:00:00+00:00"
    with DuckStore(db) as store:
        store.upsert_daily_bars(backfill)

    fetcher = _make_fetcher({"000001": _good_df()})
    run_daily_ingest(
        date=date_cls(2024, 9, 2),
        duckdb_path=db,
        universe_path=universe,
        fetcher=fetcher,
        notify_on_hard=False,
    )

    assert _count_rows(db, "000001") == 2  # backfill + new ingest


def test_run_daily_ingest_no_notify_when_no_hard(tmp_path: Path) -> None:
    """When ``notify_on_hard=True`` and there are NO hard failures,
    ``ops.notify.ding`` is NOT called (so 钉聊 never fires on
    healthy days)."""
    db = tmp_path / "test.duckdb"
    universe = _write_universe(tmp_path, [("000001", "A", "x")])
    fetcher = _make_fetcher({"000001": _good_df()})

    from ops import notify

    call_count = {"n": 0}
    real_ding = notify.ding

    def _count_ding(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        call_count["n"] += 1
        return real_ding(*args, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(notify, "ding", _count_ding)
    try:
        run_daily_ingest(
            date=date_cls(2024, 9, 2),
            duckdb_path=db,
            universe_path=universe,
            fetcher=fetcher,
            notify_on_hard=True,
        )
    finally:
        monkeypatch.undo()

    assert call_count["n"] == 0, (
        f"ding was called {call_count['n']} times on a clean run; "
        f"healthy days must not send 钉聊 alerts"
    )


# ---------------------------------------------------------------------------
# ingest_window — multi-day (window) mode (W6.1.5 refactor)
# ---------------------------------------------------------------------------


def _good_window_df(n_days: int = 5) -> pd.DataFrame:
    """n_day synthetic OHLCV with slowly increasing close, distinct dates."""
    dates = pd.bdate_range(end=pd.Timestamp("2026-01-15"), periods=n_days)
    closes = [10.00 + 0.10 * i for i in range(n_days)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": [c + 0.05 for c in closes],
            "low": [c - 0.05 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * n_days,
            "amount": [10_000_000.0] * n_days,
        }
    )


def test_ingest_window_reports_start_end_dates(tmp_path: Path) -> None:
    """``IngestReport`` carries the requested start_date / end_date
    so dashboards can label the window."""
    db = tmp_path / "test.duckdb"
    universe = _write_universe(tmp_path, [("000001", "A", "x")])
    fetcher = _make_fetcher({"000001": _good_window_df(5)})

    report = ingest_window(
        start_date=date_cls(2026, 1, 9),
        end_date=date_cls(2026, 1, 15),
        duckdb_path=db,
        universe_path=universe,
        fetcher=fetcher,
        notify_on_hard=False,
    )

    assert report.start_date == date_cls(2026, 1, 9)
    assert report.end_date == date_cls(2026, 1, 15)
    assert report.n_upserted == 1
    assert _count_rows(db, "000001") == 5


def test_ingest_window_one_call_per_symbol(tmp_path: Path) -> None:
    """Window mode makes ONE fetcher call per symbol covering the
    full window. Verifies the speed-up path: 1 call (not 7) for
    a 7-row response spanning 2026-01-09..2026-01-17."""
    db = tmp_path / "test.duckdb"
    universe = _write_universe(tmp_path, [("000001", "A", "x"), ("000002", "B", "y")])

    call_log: list[tuple[str, str, str]] = []

    def _fetch(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        call_log.append((symbol, start_date, end_date))
        df = _good_window_df(5).copy()
        df.attrs["symbol"] = symbol
        df.attrs["fetcher"] = "stub"
        df.attrs["adjust"] = "qfq"
        df.attrs["fetched_at"] = "2026-08-29T00:00:00+00:00"
        return df

    ingest_window(
        start_date=date_cls(2026, 1, 9),
        end_date=date_cls(2026, 1, 15),
        duckdb_path=db,
        universe_path=universe,
        fetcher=_fetch,
        notify_on_hard=False,
    )

    assert len(call_log) == 2, (
        f"expected 2 fetcher calls (one per symbol), got {len(call_log)}: {call_log}"
    )
    for sym, s, e in call_log:
        assert s == "20260109"
        assert e == "20260115"


def test_ingest_window_inverted_dates_raises(tmp_path: Path) -> None:
    """``start_date > end_date`` raises ``ValueError`` before any
    fetcher call (defends against typos in CLI usage)."""
    db = tmp_path / "test.duckdb"
    universe = _write_universe(tmp_path, [("000001", "A", "x")])

    with pytest.raises(ValueError, match="start_date"):
        ingest_window(
            start_date=date_cls(2026, 1, 15),
            end_date=date_cls(2026, 1, 9),
            duckdb_path=db,
            universe_path=universe,
            fetcher=_make_fetcher({}),
            notify_on_hard=False,
        )


def test_ingest_window_per_symbol_isolation(tmp_path: Path) -> None:
    """Symbol A ok, symbol B fetcher-error → A upserted, B reported."""
    db = tmp_path / "test.duckdb"
    universe = _write_universe(tmp_path, [("000001", "A", "x"), ("000002", "B", "y")])
    fetcher = _make_fetcher({"000001": _good_window_df(3), "000002": RuntimeError("boom")})

    report = ingest_window(
        start_date=date_cls(2026, 1, 13),
        end_date=date_cls(2026, 1, 15),
        duckdb_path=db,
        universe_path=universe,
        fetcher=fetcher,
        notify_on_hard=False,
    )

    assert report.n_upserted == 1
    assert report.n_other_errors == 1
    assert _count_rows(db, "000001") == 3
    assert _count_rows(db, "000002") == 0


def test_ingest_window_idempotent(tmp_path: Path) -> None:
    """Re-running for the same window does not duplicate rows
    (DuckDB upsert overwrites)."""
    db = tmp_path / "test.duckdb"
    universe = _write_universe(tmp_path, [("000001", "A", "x")])
    fetcher = _make_fetcher({"000001": _good_window_df(3)})

    ingest_window(
        start_date=date_cls(2026, 1, 13),
        end_date=date_cls(2026, 1, 15),
        duckdb_path=db,
        universe_path=universe,
        fetcher=fetcher,
        notify_on_hard=False,
    )
    ingest_window(
        start_date=date_cls(2026, 1, 13),
        end_date=date_cls(2026, 1, 15),
        duckdb_path=db,
        universe_path=universe,
        fetcher=fetcher,
        notify_on_hard=False,
    )

    assert _count_rows(db, "000001") == 3


def test_run_daily_ingest_report_has_start_eq_end(tmp_path: Path) -> None:
    """``run_daily_ingest`` (1-day mode) sets ``start_date ==
    end_date == target`` so dashboards can label uniformly across
    both flows."""
    db = tmp_path / "test.duckdb"
    universe = _write_universe(tmp_path, [("000001", "A", "x")])
    report = run_daily_ingest(
        date=date_cls(2024, 9, 2),
        duckdb_path=db,
        universe_path=universe,
        fetcher=_make_fetcher({"000001": _good_df()}),
        notify_on_hard=False,
    )
    assert report.start_date == date_cls(2024, 9, 2)
    assert report.end_date == date_cls(2024, 9, 2)
    assert report.start_date == report.end_date
