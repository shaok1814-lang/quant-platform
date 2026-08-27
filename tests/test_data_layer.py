"""Integration tests for the data layer (W2.1).

Four groups:

  * fetcher validation: symbol / adjust guards, FetcherError on empty.
  * parquet_io round-trip: write/read equivalence, missing-core raise,
    ``df.attrs`` round-trip.
  * DuckStore round-trip: upsert/query, conflict-on-upsert, date bounds.
  * End-to-end smoke (network): fetch 000001, parquet round-trip,
    DuckDB round-trip. Network test is opt-in via ``--runnetwork`` so
    CI / sandboxed runs stay hermetic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from data_layer.ingestion.akshare_fetcher import (
    ADJUST_CHOICES,
    ADJUST_QFQ,
    FetcherError,
    fetch_daily_bars,
)
from data_layer.storage.duck import DuckStore
from data_layer.storage.parquet_io import read_bars, write_bars

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _toy_bars(n_bars: int = 5) -> pd.DataFrame:
    """Build a small synthetic bars DataFrame for round-trip tests."""
    dates = pd.bdate_range(end=pd.Timestamp("2024-01-12"), periods=n_bars)
    df = pd.DataFrame(
        {
            "date": dates,
            "open": [10.0 + i * 0.1 for i in range(n_bars)],
            "high": [10.05 + i * 0.1 for i in range(n_bars)],
            "low": [9.95 + i * 0.1 for i in range(n_bars)],
            "close": [10.02 + i * 0.1 for i in range(n_bars)],
            "volume": [1_000_000.0] * n_bars,
            "amount": [10_000_000.0] * n_bars,
            "turnover": [0.5] * n_bars,
        }
    )
    df.attrs["fetcher"] = "akshare"
    df.attrs["symbol"] = "000001"
    df.attrs["adjust"] = "qfq"
    df.attrs["fetched_at"] = "2026-08-27T00:00:00+00:00"
    return df


# ===========================================================================
# Group 1: fetcher validation
# ===========================================================================


def test_fetch_daily_bars_rejects_non_six_digit_symbol() -> None:
    with pytest.raises(ValueError, match="6 digits"):
        fetch_daily_bars("abc", "20240101", "20240110")


def test_fetch_daily_bars_rejects_unknown_adjust() -> None:
    with pytest.raises(ValueError, match="adjust"):
        fetch_daily_bars("000001", "20240101", "20240110", adjust="bogus")  # type: ignore[arg-type]


def test_fetch_daily_bars_raises_on_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate akshare returning empty DataFrame — must raise FetcherError."""

    def _fake_empty(*_args: Any, **_kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame()

    monkeypatch.setattr(
        "akshare.stock_zh_a_hist", _fake_empty, raising=True
    )
    with pytest.raises(FetcherError, match="no rows"):
        fetch_daily_bars("000001", "20240101", "20240110")


def test_adjust_choices_match_documented_values() -> None:
    assert ADJUST_CHOICES == ("qfq", "hfq", "")
    assert ADJUST_QFQ == "qfq"


# ===========================================================================
# Group 2: parquet round-trip
# ===========================================================================


def test_parquet_round_trip_preserves_data(tmp_path: Path) -> None:
    src = _toy_bars(5)
    p = tmp_path / "000001.parquet"
    write_bars(p, src)
    out = read_bars(p)
    pd.testing.assert_frame_equal(
        out.reset_index(drop=True),
        src.reset_index(drop=True),
        check_dtype=False,
    )


def test_parquet_write_rejects_missing_core_column(tmp_path: Path) -> None:
    df = _toy_bars(3).drop(columns=["close"])
    p = tmp_path / "000001.parquet"
    with pytest.raises(ValueError, match="core columns"):
        write_bars(p, df)


def test_parquet_attrs_round_trip(tmp_path: Path) -> None:
    src = _toy_bars(3)
    p = tmp_path / "000001.parquet"
    write_bars(p, src)
    out = read_bars(p)
    # pyarrow preserves df.attrs under the "pandas" metadata key;
    # pd.read_parquet auto-restores it.
    assert out.attrs.get("fetcher") == "akshare"
    assert out.attrs.get("symbol") == "000001"
    assert out.attrs.get("adjust") == "qfq"


def test_parquet_read_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_bars(tmp_path / "does_not_exist.parquet")


# ===========================================================================
# Group 3: DuckStore round-trip
# ===========================================================================


def test_duck_round_trip_returns_same_rows(tmp_path: Path) -> None:
    src = _toy_bars(5)
    db = tmp_path / "daily.duckdb"
    with DuckStore(db) as store:
        store.upsert_daily_bars(src)
        out = store.query_daily_bars("000001")
    assert len(out) == len(src)
    assert list(out["symbol"].unique()) == ["000001"]
    # DuckDB DATE column returns pd.Timestamp on read, not datetime.date.
    assert out["date"].iloc[0] == pd.Timestamp("2024-01-08")


def test_duck_upsert_overwrites_on_conflict(tmp_path: Path) -> None:
    src = _toy_bars(3)
    db = tmp_path / "daily.duckdb"
    with DuckStore(db) as store:
        store.upsert_daily_bars(src)
        # Re-upsert with bumped close and new fetched_at.
        bumped = src.copy()
        bumped["close"] = bumped["close"] + 100.0
        bumped.attrs["fetched_at"] = "2026-08-27T12:00:00+00:00"
        store.upsert_daily_bars(bumped)
        out = store.query_daily_bars("000001")
    assert len(out) == 3  # no row duplication on conflict
    assert out["close"].iloc[0] == pytest.approx(110.02, abs=1e-6)
    assert out["fetched_at"].iloc[0] == "2026-08-27T12:00:00+00:00"


def test_duck_query_date_bounds(tmp_path: Path) -> None:
    src = _toy_bars(5)
    db = tmp_path / "daily.duckdb"
    with DuckStore(db) as store:
        store.upsert_daily_bars(src)
        # First bar is 2024-01-08 (Mon). Bound by Jan 10 → 3 rows.
        out = store.query_daily_bars("000001", "2024-01-08", "2024-01-10")
    assert len(out) == 3


def test_duck_multiple_symbols_coexist(tmp_path: Path) -> None:
    a = _toy_bars(3)
    b = _toy_bars(3)
    a.attrs["symbol"] = "000001"
    b.attrs["symbol"] = "600000"
    db = tmp_path / "daily.duckdb"
    with DuckStore(db) as store:
        store.upsert_daily_bars(a)
        store.upsert_daily_bars(b)
        out_a = store.query_daily_bars("000001")
        out_b = store.query_daily_bars("600000")
    assert len(out_a) == 3 and len(out_b) == 3
    assert set(out_a["symbol"].unique()) == {"000001"}
    assert set(out_b["symbol"].unique()) == {"600000"}


def test_duck_upsert_requires_symbol_attr(tmp_path: Path) -> None:
    src = _toy_bars(2)
    src.attrs.pop("symbol")
    db = tmp_path / "daily.duckdb"
    with DuckStore(db) as store, pytest.raises(ValueError, match="symbol"):
        store.upsert_daily_bars(src)


# ===========================================================================
# Group 4: end-to-end network smoke (opt-in)
# ===========================================================================


@pytest.mark.skip(
    reason="network test — requires akshare access. Run with "
    "`uv run pytest -m 'not skip' -k network_smoke` after manual opt-in."
)
def test_network_smoke_000001_round_trip(tmp_path: Path) -> None:
    """Fetch 000001, round-trip through parquet and DuckDB."""
    df = fetch_daily_bars("000001", "20240901", "20260825")
    assert len(df) > 100  # sanity: 2y of daily bars

    p = tmp_path / "000001.parquet"
    write_bars(p, df)
    rd = read_bars(p)
    assert len(rd) == len(df)

    db = tmp_path / "daily.duckdb"
    with DuckStore(db) as store:
        store.upsert_daily_bars(rd)
        out = store.query_daily_bars("000001")
    assert len(out) == len(df)
