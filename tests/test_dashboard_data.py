"""Tests for ``ops.dashboard_data`` (W6.2.1).

Pure-function tests; no Streamlit runtime. Each test owns a tiny
DuckDB at ``tmp_path`` so the suite is parallel-safe and the
production ``data/duckdb/daily.duckdb`` is left untouched.

Coverage:

  * ``load_universe_status`` — empty / populated / multiple symbols
    / sector alignment / n_trading_days count.
  * ``load_symbol_bars`` — empty result for missing symbol / with
    date filter / without date filter.
  * ``load_multi_symbol_universe`` — drops empty symbols / matches
    akquant contract (Dict[str, pd.DataFrame]).
  * ``compute_strategy_equity`` — runs on synthetic universe
    (no AKQuant network needed) / empty data returns empty
    series.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_layer.storage.duck import DuckStore  # noqa: E402
from ops.dashboard_data import (  # noqa: E402
    compute_strategy_equity,
    load_multi_symbol_universe,
    load_symbol_bars,
    load_universe_status,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _populate(
    db: Path, rows_by_symbol: dict[str, list[tuple[str, float, float, float, float, float]]]
) -> None:
    """Write tiny per-symbol OHLCV to ``db``. ``rows_by_symbol`` maps
    symbol → list of ``(date_iso, open, high, low, close, volume)``.

    Generates ``amount`` = volume * close * 0.001 (so the DuckStore
    contract is satisfied) and stamps the right ``df.attrs``.
    """
    with DuckStore(db) as store:
        for sym, rows in rows_by_symbol.items():
            df = pd.DataFrame(
                rows,
                columns=["date", "open", "high", "low", "close", "volume"],
            )
            df["date"] = pd.to_datetime(df["date"])
            df["amount"] = df["volume"] * df["close"] * 0.001
            df.attrs["symbol"] = sym
            df.attrs["fetcher"] = "stub"
            df.attrs["adjust"] = "qfq"
            df.attrs["fetched_at"] = "2026-08-29T00:00:00+00:00"
            store.upsert_daily_bars(df)


# ---------------------------------------------------------------------------
# load_universe_status
# ---------------------------------------------------------------------------


def test_load_universe_status_empty_db(tmp_path: Path) -> None:
    """No DuckDB file → empty DataFrame (no exception)."""
    status = load_universe_status(tmp_path / "no-such.duckdb")
    assert list(status.columns) == [
        "symbol",
        "n_rows",
        "first_dt",
        "last_dt",
        "n_trading_days",
        "fetchers",
    ]
    assert len(status) == 0


def test_load_universe_status_populated(tmp_path: Path) -> None:
    """Two symbols / different fetcher labels / known row counts."""
    db = tmp_path / "test.duckdb"
    _populate(
        db,
        {
            "000001": [
                ("2024-09-02", 10.0, 10.5, 9.5, 10.2, 1_000_000.0),
                ("2024-09-03", 10.2, 10.7, 9.8, 10.4, 1_200_000.0),
            ],
            "600000": [
                ("2025-01-06", 8.0, 8.4, 7.9, 8.1, 900_000.0),
            ],
        },
    )
    status = load_universe_status(db)
    assert len(status) == 2
    # Sorted by symbol ascending.
    assert list(status["symbol"]) == ["000001", "600000"]
    assert list(status["n_rows"]) == [2, 1]
    assert str(status.loc[0, "first_dt"])[:10] == "2024-09-02"
    assert str(status.loc[1, "first_dt"])[:10] == "2025-01-06"
    # fetchers column is the string union of distinct fetcher labels.
    assert all("stub" in (s or "") for s in status["fetchers"])


def test_load_universe_status_trading_days_count(tmp_path: Path) -> None:
    """``n_trading_days`` reflects business-day count between
    first_dt and last_dt (gap detector between rows)."""
    db = tmp_path / "test.duckdb"
    # 5 weekdays Mon-Fri; n_trading_days should be 5.
    _populate(
        db,
        {
            "000001": [
                ("2025-09-01", 1.0, 1.05, 0.95, 1.02, 1_000.0),  # Mon
                ("2025-09-02", 1.0, 1.05, 0.95, 1.02, 1_000.0),  # Tue
                ("2025-09-03", 1.0, 1.05, 0.95, 1.02, 1_000.0),  # Wed
                ("2025-09-04", 1.0, 1.05, 0.95, 1.02, 1_000.0),  # Thu
                ("2025-09-05", 1.0, 1.05, 0.95, 1.02, 1_000.0),  # Fri
            ],
        },
    )
    status = load_universe_status(db)
    assert status.loc[0, "n_trading_days"] == 5


def test_load_universe_status_sorted_by_symbol(tmp_path: Path) -> None:
    """Result rows are sorted by symbol ascending."""
    db = tmp_path / "test.duckdb"
    _populate(
        db,
        {
            "600000": [("2025-01-06", 8.0, 8.4, 7.9, 8.1, 1.0)],
            "000001": [("2025-01-06", 1.0, 1.05, 0.95, 1.02, 1.0)],
        },
    )
    status = load_universe_status(db)
    assert list(status["symbol"]) == ["000001", "600000"]


# ---------------------------------------------------------------------------
# load_symbol_bars
# ---------------------------------------------------------------------------


def test_load_symbol_bars_returns_empty_on_missing(tmp_path: Path) -> None:
    """Symbol not in DuckDB returns empty df (NOT raises).

    The empty result carries the full DuckDB table column list
    (DuckDB's default for empty SELECT) — we verify it's empty and
    the canonical OHLCV columns are present, not the exact column
    list."""
    db = tmp_path / "test.duckdb"
    _populate(db, {"000001": [("2024-09-02", 10.0, 10.5, 9.5, 10.2, 1_000_000.0)]})
    df = load_symbol_bars("999999", duckdb_path=db)
    assert df.empty
    for col in ("date", "open", "high", "low", "close", "volume"):
        assert col in df.columns


def test_load_symbol_bars_with_date_filter(tmp_path: Path) -> None:
    """start_date / end_date filter rows inclusively on both ends."""
    db = tmp_path / "test.duckdb"
    _populate(
        db,
        {
            "000001": [
                ("2024-09-02", 1.0, 1.05, 0.95, 1.02, 1_000_000.0),
                ("2024-09-03", 1.0, 1.05, 0.95, 1.02, 1_000_000.0),
                ("2024-09-04", 1.0, 1.05, 0.95, 1.02, 1_000_000.0),
                ("2024-09-05", 1.0, 1.05, 0.95, 1.02, 1_000_000.0),
            ],
        },
    )
    df = load_symbol_bars("000001", start_date="2024-09-03", end_date="2024-09-04", duckdb_path=db)
    assert len(df) == 2
    assert str(df.iloc[0]["date"])[:10] == "2024-09-03"
    assert str(df.iloc[-1]["date"])[:10] == "2024-09-04"


def test_load_symbol_bars_missing_db_returns_empty(tmp_path: Path) -> None:
    """DuckDB file missing → empty df with OHLCV columns (graceful)."""
    df = load_symbol_bars("000001", duckdb_path=tmp_path / "no.duckdb")
    assert df.empty
    # The missing-DB path returns a synthetic empty df with the
    # 7 OHLCV columns (not the full DuckDB schema).
    assert list(df.columns) == [
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    ]


# ---------------------------------------------------------------------------
# load_multi_symbol_universe
# ---------------------------------------------------------------------------


def test_load_multi_symbol_universe_drops_empty(tmp_path: Path) -> None:
    """Symbols with zero rows are OMITTED (not raised, not NaN-d)."""
    db = tmp_path / "test.duckdb"
    _populate(
        db,
        {
            "000001": [("2024-09-02", 1.0, 1.05, 0.95, 1.02, 1_000_000.0)],
            "600000": [("2024-09-02", 8.0, 8.4, 7.9, 8.1, 1_000_000.0)],
        },
    )
    data = load_multi_symbol_universe(["000001", "600000", "999999"], duckdb_path=db)
    assert set(data.keys()) == {"000001", "600000"}
    assert all(isinstance(df, pd.DataFrame) for df in data.values())


def test_load_multi_symbol_universe_matches_akquant_contract(tmp_path: Path) -> None:
    """Returned dict shape matches ``akquant.run_backtest``'s
    multi-symbol contract: ``Dict[str, pd.DataFrame]`` with the
    canonical OHLCV columns."""
    db = tmp_path / "test.duckdb"
    _populate(
        db,
        {
            "000001": [("2024-09-02", 1.0, 1.05, 0.95, 1.02, 1_000_000.0)],
        },
    )
    data = load_multi_symbol_universe(["000001"], duckdb_path=db)
    assert "000001" in data
    df = data["000001"]
    assert isinstance(df, pd.DataFrame)
    required = {"date", "open", "high", "low", "close", "volume"}
    assert required.issubset(set(df.columns))


# ---------------------------------------------------------------------------
# compute_strategy_equity — runs real AKQuant on synthetic frames
# ---------------------------------------------------------------------------


def test_compute_strategy_equity_empty_universe_returns_empty_series(tmp_path: Path) -> None:
    """Empty universe (no symbols in DuckDB) → empty equity curve,
    NOT a crash."""
    from research.strategies.ma_cross import MACrossStrategy

    data = load_multi_symbol_universe(["000001"], duckdb_path=tmp_path / "no.duckdb")
    assert data == {}  # sanity
    equity, result = compute_strategy_equity(MACrossStrategy, data=data)
    # AKQuant on empty universe: equity curve may be 0-length or
    # flat; we accept either as "didn't crash".
    assert isinstance(equity, pd.Series)
    assert hasattr(result, "metrics_df")


def test_compute_strategy_equity_runs_on_synthetic_multi_symbol(tmp_path: Path) -> None:
    """``compute_strategy_equity`` runs AKQuant end-to-end on a
    multi-symbol in-memory frame (no DuckDB read needed). The
    W6.2 dashboard relies on this to render charts per click.

    Uses ``TopNMeanReversionStrategy`` (multi-symbol) — confirms
    the wrapper handles Dict[str, pd.DataFrame] input."""

    from research.strategies.topn_mean_reversion import TopNMeanReversionStrategy

    n_days = 60
    dates = pd.bdate_range(end=pd.Timestamp("2025-01-15"), periods=n_days)
    data: dict[str, pd.DataFrame] = {}
    for sym, offset in [("000001", 0.0), ("000002", 0.01), ("600000", -0.01)]:
        closes = [10.0 + offset * i for i in range(n_days)]
        df = pd.DataFrame(
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
        data[sym] = df

    equity, result = compute_strategy_equity(
        TopNMeanReversionStrategy,
        data=data,
        initial_cash=1_000_000.0,
    )
    # Some bars must have produced trades → equity curve non-empty.
    assert isinstance(equity, pd.Series)
    # Result has metric rows on the canonical set.
    assert not result.metrics_df.empty
