"""Tests for the universe-level A-share hooks (W7.1 follow-up).

Covers :func:`ops.universe.load_filtered_universe` integration with
``filter_st`` + ``build_universe`` from ``backtest.a_share``.

The snapshot CSVs ``data/st_a_share_list.csv`` (203 symbols) and
``data/delisted_a_share_list.csv`` (361 symbols) are read by the
helper. To avoid coupling tests to the snapshot's exact counts,
each test passes an injected ``st_set`` / ``delisted_set`` via
the underlying ``filter_st`` / ``build_universe`` instead.

For the integration smoke (``test_load_filtered_universe_includes_delisted``),
we use the real snapshots because they are committed to ``data/``.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from backtest.a_share import (
    build_universe,
    filter_st,
)
from ops.universe import load_filtered_universe


def _write_yaml(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    p = tmp_path / "universe.yaml"
    yaml = "universe:\n"
    for sym, name, sector in rows:
        yaml += f'  - {{symbol: "{sym}", name: "{name}", sector: "{sector}"}}\n'
    p.write_text(yaml, encoding="utf-8")
    return p


def test_load_filtered_universe_keeps_all_yaml_when_st_clean_and_no_delisted(
    tmp_path: Path,
) -> None:
    """Default include_st=False + include_delisted=False → YAML untouched."""
    p = _write_yaml(
        tmp_path,
        [("000001", "Ping An", "bank"), ("600519", "Kweichow", "consumer")],
    )
    out = load_filtered_universe(p, include_st=False, include_delisted=False)
    assert [e.symbol for e in out] == ["000001", "600519"]
    assert all(e.sector != "delisted" for e in out)


def test_load_filtered_universe_drops_st_symbols(
    tmp_path: Path,
) -> None:
    """ST codes passed in via injected st_set are filtered out."""
    p = _write_yaml(
        tmp_path,
        [
            ("000001", "Ping An", "bank"),
            ("000010", "ST Sample", "industrial"),
            ("600519", "Kweichow", "consumer"),
        ],
    )
    # Patch at the source module (``backtest.a_share``) because
    # ``load_filtered_universe`` does a lazy local-scope import
    # of ``fetch_st_symbols`` / ``fetch_delisted_symbols``.
    from backtest import a_share as a_share_mod

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(a_share_mod, "fetch_st_symbols", lambda **kw: {"000010"})
        monkey.setattr(a_share_mod, "fetch_delisted_symbols", lambda **kw: set())
        out = load_filtered_universe(p, include_st=False, include_delisted=False)
    finally:
        monkey.undo()
    assert [e.symbol for e in out] == ["000001", "600519"]


def test_load_filtered_universe_appends_delisted_with_placeholder(
    tmp_path: Path,
) -> None:
    """Delisted codes get name='[delisted]' + sector='delisted' placeholder."""
    p = _write_yaml(tmp_path, [("000001", "Ping An", "bank")])
    from backtest import a_share as a_share_mod

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(a_share_mod, "fetch_st_symbols", lambda **kw: set())
        monkey.setattr(a_share_mod, "fetch_delisted_symbols", lambda **kw: {"999999"})
        out = load_filtered_universe(p, include_st=False, include_delisted=True)
    finally:
        monkey.undo()
    assert [e.symbol for e in out] == ["000001", "999999"]
    delisted_entry = next(e for e in out if e.symbol == "999999")
    assert delisted_entry.name == "[delisted]"
    assert delisted_entry.sector == "delisted"


def test_load_filtered_universe_combines_filter_and_include(
    tmp_path: Path,
) -> None:
    """Combined: ST dropped, delisted appended, both from injected sets."""
    p = _write_yaml(
        tmp_path,
        [("000001", "Ping An", "bank"), ("000010", "ST Sample", "industrial")],
    )
    from backtest import a_share as a_share_mod

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(a_share_mod, "fetch_st_symbols", lambda **kw: {"000010"})
        monkey.setattr(a_share_mod, "fetch_delisted_symbols", lambda **kw: {"999999"})
        out = load_filtered_universe(p, include_st=False, include_delisted=True)
    finally:
        monkey.undo()
    symbols = [e.symbol for e in out]
    assert "000001" in symbols  # active kept
    assert "000010" not in symbols  # ST dropped
    assert "999999" in symbols  # delisted appended


def test_load_filtered_universe_integration_with_real_snapshots(tmp_path: Path) -> None:
    """Smoke test: real ``data/*.csv`` snapshots produce non-empty result.

    The CSV snapshots are committed; this test confirms the wiring
    works against the actual data files. Counts are not asserted —
    if the snapshot grows or shrinks (monthly refresh), this test
    still passes as long as the offline reader is wired correctly.
    """
    p = _write_yaml(tmp_path, [("000001", "Ping An", "bank")])
    out = load_filtered_universe(p)  # uses real snapshots, no monkeypatch
    # The active '000001' from YAML must always be present.
    assert any(e.symbol == "000001" for e in out)
    # The delisted snapshot is non-empty (>= 1 symbol).
    assert any(e.sector == "delisted" for e in out)


# --- Pure-function regression (filter_st + build_universe) ---


def test_filter_st_drops_by_default() -> None:
    out = filter_st(["000001", "000010", "600519"], st_set={"000010"})
    assert out == ["000001", "600519"]


def test_filter_st_keeps_when_include_st() -> None:
    out = filter_st(["000001", "000010"], st_set={"000010"}, include_st=True)
    assert out == ["000001", "000010"]


def test_build_universe_includes_delisted_by_default() -> None:
    out = build_universe(["000001"], delisted_set={"999999"})
    assert "000001" in out and "999999" in out
