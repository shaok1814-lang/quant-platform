"""Tests for ``research/strategies/_multi_symbol_loader.py`` (W3.2-C1)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from data_layer.storage.duck import DuckStore
from research.strategies._multi_symbol_loader import load_multi_symbol_bars
from tests.conftest import make_bars


def _seed_duckdb(tmp_path: Path, frames: dict[str, pd.DataFrame]) -> Path:
    """Upsert the given ``{symbol: frame}`` mapping into a fresh
    DuckDB file at ``tmp_path/daily.duckdb``. Returns the path."""
    db_path = tmp_path / "daily.duckdb"
    with DuckStore(db_path) as store:
        for sym, df in frames.items():
            store.upsert_daily_bars(df)
    return db_path


# ===========================================================================
# Group 1: happy path
# ===========================================================================


def test_load_multi_symbol_bars_returns_dict_of_frames(tmp_path: Path) -> None:
    """4-symbol universe round-trips; the dict shape is what
    ``run_backtest`` accepts."""
    frames = {
        sym: make_bars([10.0 + i for i in range(20)], symbol=sym)
        for sym in ("000001", "600000", "000002", "600519")
    }
    db_path = _seed_duckdb(tmp_path, frames)
    out = load_multi_symbol_bars(db_path, list(frames.keys()))
    assert set(out.keys()) == set(frames.keys())
    for sym in frames:
        assert len(out[sym]) == 20
        assert (out[sym]["symbol"] == sym).all()


# ===========================================================================
# Group 2: missing-symbol behaviour
# ===========================================================================


def test_load_multi_symbol_bars_drops_missing_symbols(tmp_path: Path) -> None:
    """A symbol with no DuckDB rows is silently dropped."""
    db_path = _seed_duckdb(tmp_path, {"000001": make_bars([10.0] * 5, symbol="000001")})
    out = load_multi_symbol_bars(db_path, ["000001", "999999"])
    assert set(out.keys()) == {"000001"}
    assert "999999" not in out


def test_load_multi_symbol_bars_empty_symbol_list(tmp_path: Path) -> None:
    """Empty ``symbols`` list → empty dict without touching DuckDB."""
    db_path = tmp_path / "irrelevant.duckdb"
    out = load_multi_symbol_bars(db_path, [])
    assert out == {}


def test_load_multi_symbol_bars_db_with_no_data(tmp_path: Path) -> None:
    """A fresh DuckDB file with no upserts → empty dict for any query."""
    db_path = tmp_path / "empty.duckdb"
    out = load_multi_symbol_bars(db_path, ["000001", "600000"])
    assert out == {}


# ===========================================================================
# Group 3: date bounds
# ===========================================================================


def test_load_multi_symbol_bars_respects_date_bounds(tmp_path: Path) -> None:
    """``start_date`` / ``end_date`` narrow the per-symbol query."""
    df = make_bars([10.0 + i for i in range(30)], symbol="000001")
    db_path = _seed_duckdb(tmp_path, {"000001": df})
    # ISO bounds slice the bdate_range to a 10-bar window.
    out = load_multi_symbol_bars(
        db_path,
        ["000001"],
        start_date="2024-01-08",
        end_date="2024-01-22",
    )
    assert set(out.keys()) == {"000001"}
    assert len(out["000001"]) == 11  # inclusive on both ends


def test_load_multi_symbol_bars_open_ended_bounds(tmp_path: Path) -> None:
    """``None`` bounds return all rows for the symbol."""
    df = make_bars([10.0 + i for i in range(15)], symbol="000001")
    db_path = _seed_duckdb(tmp_path, {"000001": df})
    out = load_multi_symbol_bars(db_path, ["000001"])
    assert len(out["000001"]) == 15


# ===========================================================================
# Group 4: cross-symbol metadata (via the ``symbol`` column, not attrs)
# ===========================================================================


def test_load_multi_symbol_bars_preserves_symbol_column(tmp_path: Path) -> None:
    """``DuckStore.query_daily_bars`` writes the symbol into the
    ``symbol`` column (the contract that ``upsert_daily_bars``
    enforces). The loader preserves it on the way out so
    AKQuant's per-symbol routing works without surprises.

    Note: ``df.attrs`` is NOT round-tripped — it is an in-memory
    pandas metadata container and DuckDB does not persist it. This
    is a DuckStore implementation detail, not a loader bug; callers
    that need ``attrs`` should set them after the load.
    """
    df_a = make_bars([10.0] * 5, symbol="000001")
    df_b = make_bars([20.0] * 5, symbol="600000")
    db_path = _seed_duckdb(tmp_path, {"000001": df_a, "600000": df_b})
    out = load_multi_symbol_bars(db_path, ["000001", "600000"])
    assert (out["000001"]["symbol"] == "000001").all()
    assert (out["600000"]["symbol"] == "600000").all()


# ===========================================================================
# Group 5: input shape flexibility
# ===========================================================================


def test_load_multi_symbol_bars_accepts_tuple(tmp_path: Path) -> None:
    """``symbols`` may be a tuple as well as a list."""
    db_path = _seed_duckdb(tmp_path, {"000001": make_bars([10.0] * 5, symbol="000001")})
    out = load_multi_symbol_bars(db_path, ("000001",))
    assert set(out.keys()) == {"000001"}


def test_load_multi_symbol_bars_str_path(tmp_path: Path) -> None:
    """``duckdb_path`` accepts a plain string (no Path required)."""
    db_path = _seed_duckdb(tmp_path, {"000001": make_bars([10.0] * 5, symbol="000001")})
    out = load_multi_symbol_bars(str(db_path), ["000001"])
    assert set(out.keys()) == {"000001"}
