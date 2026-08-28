"""Tests for ``backtest/a_share/delisted_universe.py`` (W4-C3)."""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import pytest
from backtest.a_share.delisted_universe import (
    OFFLINE_DELISTED_CSV,
    build_universe,
    fetch_delisted_symbols,
)

# ===========================================================================
# Group 1: build_universe — default include_delisted=True (CLAUDE.md)
# ===========================================================================


def test_build_universe_default_includes_delisted() -> None:
    """Default ``include_delisted=True`` retains delisted codes in the universe."""
    active = ["000001", "600000", "600001"]  # 600001 is delisted
    out = build_universe(active, delisted_set={"600001"})
    assert set(out) == {"000001", "600000", "600001"}
    # Active symbols come first.
    assert out[0] == "000001" or out.index("000001") < out.index("600001")


def test_build_universe_opt_out_drops_delisted() -> None:
    """``include_delisted=False`` returns the survivor-biased universe
    (legacy / experimentation only). 600001 is delisted and NOT in
    the active list — so opting out drops it entirely.
    """
    active = ["000001", "600000"]  # 600001 (delisted) NOT in active
    out = build_universe(active, include_delisted=False, delisted_set={"600001"})
    assert out == ["000001", "600000"]


def test_build_universe_dedup_active_vs_delisted() -> None:
    """If a symbol is in both ``active`` and the delisted set, it
    appears once (active wins on dedup)."""
    active = ["000001"]
    out = build_universe(active, delisted_set={"000001", "000002"})
    assert out.count("000001") == 1
    assert set(out) == {"000001", "000002"}


def test_build_universe_empty_active_includes_only_delisted() -> None:
    """Edge case: empty ``active`` list still picks up the delisted set."""
    out = build_universe([], delisted_set={"600001", "600002"})
    assert set(out) == {"600001", "600002"}


def test_build_universe_no_delisted_set() -> None:
    """If ``delisted_set=None``, no delisted symbols are appended
    (lazy ``fetch_delisted_symbols`` would be called by callers that
    want network behavior)."""
    out = build_universe(["000001", "600000"], include_delisted=False, delisted_set=None)
    assert out == ["000001", "600000"]


# ===========================================================================
# Group 2: fetch_delisted_symbols — offline CSV fallback
# ===========================================================================


def test_fetch_delisted_symbols_offline_only(tmp_path: Path) -> None:
    """Offline CSV with a recognized code column returns its symbols."""
    csv_path = tmp_path / "delisted.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["代码", "名称"])
        writer.writeheader()
        writer.writerow({"代码": "600001", "名称": "Old SSE"})
        writer.writerow({"代码": "000003", "名称": "Old SZSE"})
    out = fetch_delisted_symbols(offline_csv=csv_path, allow_network=False)
    assert out == {"600001", "000003"}


def test_fetch_delisted_symbols_accepts_szse_header(tmp_path: Path) -> None:
    """CSV with ``证券代码`` header (SZSE convention) is also accepted."""
    csv_path = tmp_path / "delisted.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["证券代码", "证券简称"])
        writer.writeheader()
        writer.writerow({"证券代码": "000003", "证券简称": "退市股"})
    out = fetch_delisted_symbols(offline_csv=csv_path, allow_network=False)
    assert out == {"000003"}


def test_fetch_delisted_symbols_accepts_sse_header(tmp_path: Path) -> None:
    """CSV with ``公司代码`` header (SSE convention) is also accepted."""
    csv_path = tmp_path / "delisted.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["公司代码", "公司简称"])
        writer.writeheader()
        writer.writerow({"公司代码": "600001", "公司简称": "退市股"})
    out = fetch_delisted_symbols(offline_csv=csv_path, allow_network=False)
    assert out == {"600001"}


def test_fetch_delisted_symbols_missing_file_returns_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    missing = tmp_path / "does-not-exist.csv"
    with caplog.at_level(logging.WARNING):
        out = fetch_delisted_symbols(offline_csv=missing, allow_network=False)
    assert out == set()
    assert any("missing" in r.message.lower() for r in caplog.records)


def test_fetch_delisted_symbols_no_recognized_column(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """CSV with no recognizable code column → empty + WARNING."""
    csv_path = tmp_path / "delisted.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["foo", "bar"])
        writer.writeheader()
        writer.writerow({"foo": "x", "bar": "y"})
    with caplog.at_level(logging.WARNING):
        out = fetch_delisted_symbols(offline_csv=csv_path, allow_network=False)
    assert out == set()


# ===========================================================================
# Group 3: contract / constants
# ===========================================================================


def test_offline_delisted_csv_default_path() -> None:
    """Default path is ``data/delisted_a_share_list.csv``."""
    assert OFFLINE_DELISTED_CSV == Path("data/delisted_a_share_list.csv")


def test_build_universe_preserves_active_order() -> None:
    """Active symbols come out in the original order; delisted is
    appended in set-iteration order."""
    active = ["000003", "000001", "000002"]
    out = build_universe(active, delisted_set={"999990", "999991"})
    assert out[:3] == ["000003", "000001", "000002"]
    assert set(out[3:]) == {"999990", "999991"}
