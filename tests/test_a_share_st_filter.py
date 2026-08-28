"""Tests for ``backtest/a_share/st_filter.py`` (W4-C3)."""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import pytest
from backtest.a_share.st_filter import (
    OFFLINE_ST_CSV,
    fetch_st_symbols,
    filter_st,
)

# ===========================================================================
# Group 1: filter_st — pure function tests with injected st_set
# ===========================================================================


def test_filter_st_drops_st_by_default() -> None:
    """``include_st=False`` (default per CLAUDE.md) drops ST symbols."""
    universe = ["000001", "600000", "600519", "000002"]
    out = filter_st(universe, st_set={"600519"})
    assert out == ["000001", "600000", "000002"]


def test_filter_st_opt_in_keeps_st() -> None:
    """``include_st=True`` keeps ST symbols."""
    universe = ["000001", "600000", "600519", "000002"]
    out = filter_st(universe, include_st=True, st_set={"600519"})
    assert out == universe


def test_filter_st_empty_input() -> None:
    """Empty universe returns ``[]`` (no raise)."""
    out = filter_st([], st_set={"600519"})
    assert out == []


def test_filter_st_empty_st_set() -> None:
    """Empty ``st_set`` returns the universe unchanged (no ST symbols
    to drop)."""
    universe = ["000001", "600000", "600002"]
    out = filter_st(universe, st_set=set())
    assert out == universe


def test_filter_st_preserves_order() -> None:
    """Output preserves the input order."""
    universe = ["000003", "000001", "000002"]
    out = filter_st(universe, st_set={"000002"})
    assert out == ["000003", "000001"]


def test_filter_st_all_st_drops_all() -> None:
    """If every symbol is in ``st_set``, the result is empty."""
    universe = ["000001", "000002"]
    out = filter_st(universe, st_set={"000001", "000002"})
    assert out == []


# ===========================================================================
# Group 2: fetch_st_symbols — offline CSV fallback
# ===========================================================================


def test_fetch_st_symbols_offline_only(tmp_path: Path) -> None:
    """``allow_network=False`` + a valid offline CSV returns its symbols."""
    csv_path = tmp_path / "st.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["代码", "名称"])
        writer.writeheader()
        writer.writerow({"代码": "600519", "名称": "ST Sample"})
        writer.writerow({"代码": "000001", "名称": "Another ST"})
    out = fetch_st_symbols(offline_csv=csv_path, allow_network=False)
    assert out == {"600519", "000001"}


def test_fetch_st_symbols_normalizes_int_codes(tmp_path: Path) -> None:
    """CSV with int codes (e.g. ``1``) is normalized to ``000001``."""
    csv_path = tmp_path / "st.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["代码"])
        writer.writeheader()
        writer.writerow({"代码": 1})
        writer.writerow({"代码": "600519"})
    out = fetch_st_symbols(offline_csv=csv_path, allow_network=False)
    assert out == {"000001", "600519"}


def test_fetch_st_symbols_missing_file_returns_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing offline CSV + ``allow_network=False`` → empty set + WARNING."""
    missing = tmp_path / "does-not-exist.csv"
    with caplog.at_level(logging.WARNING):
        out = fetch_st_symbols(offline_csv=missing, allow_network=False)
    assert out == set()
    assert any("missing" in r.message.lower() for r in caplog.records)


def test_fetch_st_symbols_wrong_column_returns_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """CSV with the wrong column header → empty set + WARNING."""
    csv_path = tmp_path / "st.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "name"])
        writer.writeheader()
        writer.writerow({"symbol": "000001", "name": "X"})
    with caplog.at_level(logging.WARNING):
        out = fetch_st_symbols(offline_csv=csv_path, allow_network=False)
    assert out == set()
    assert any("missing column" in r.message.lower() for r in caplog.records)


def test_fetch_st_symbols_offline_none_skips_csv_path() -> None:
    """``offline_csv=None`` skips the CSV path entirely (does not raise)."""
    # allow_network=False so we don't hit akshare in CI.
    out = fetch_st_symbols(offline_csv=None, allow_network=False)
    assert out == set()


# ===========================================================================
# Group 3: contract / constants
# ===========================================================================


def test_offline_st_csv_default_path() -> None:
    """Default path is ``data/st_a_share_list.csv`` (relative to project root)."""
    assert OFFLINE_ST_CSV == Path("data/st_a_share_list.csv")


def test_filter_st_calls_fetch_when_st_set_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``st_set=None`` triggers ``fetch_st_symbols`` (lazy network)."""
    called: dict[str, bool] = {"hit": False}

    def fake_fetch(*, offline_csv: Path | None = None, allow_network: bool = True) -> set[str]:
        called["hit"] = True
        return {"600519"}

    monkeypatch.setattr("backtest.a_share.st_filter.fetch_st_symbols", fake_fetch)
    out = filter_st(["000001", "600519"], st_set=None)
    assert out == ["000001"]
    assert called["hit"] is True
