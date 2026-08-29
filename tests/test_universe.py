"""Tests for ``ops.universe.load_universe`` (W6.1.1).

Validates:

  * Default file loads cleanly and returns the expected count.
  * Symbols are 6-digit, unique, sorted by symbol.
  * Bad YAML / bad entry / duplicate symbol / missing keys all
    raise ``ValueError``.
  * Missing file raises ``FileNotFoundError``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ops.universe import DEFAULT_UNIVERSE_PATH, UniverseEntry, load_universe  # noqa: E402


def test_load_universe_default_file_succeeds() -> None:
    """Default ``config/universe.yaml`` loads without errors."""
    entries = load_universe()
    assert len(entries) >= 30, (
        f"universe should have >= 30 symbols per W6.1 plan, got {len(entries)}"
    )
    assert all(isinstance(e, UniverseEntry) for e in entries)
    assert all(len(e.symbol) == 6 and e.symbol.isdigit() for e in entries)


def test_load_universe_sorted_by_symbol_and_unique() -> None:
    """Loaded list is sorted by symbol (deterministic order)
    and contains no duplicates."""
    entries = load_universe()
    symbols = [e.symbol for e in entries]
    assert symbols == sorted(symbols), "universe must be sorted by symbol"
    assert len(set(symbols)) == len(symbols), "universe must have unique symbols"


def test_load_universe_has_required_sectors() -> None:
    """Sanity: at least one entry from each major sector tag."""
    entries = load_universe()
    sectors = {e.sector for e in entries}
    # Loose check — at least 5 sectors represented.
    assert len(sectors) >= 5, f"too few sectors: {sectors}"
    # Spot-check: 'bank', 'etf' should both be present for W6.1
    # liquid-blue-chip baseline.
    assert "bank" in sectors, "missing bank sector"
    assert "etf" in sectors, "missing etf sector"


def test_load_universe_missing_file_raises(tmp_path: Path) -> None:
    """Non-existent path raises ``FileNotFoundError``."""
    with pytest.raises(FileNotFoundError):
        load_universe(tmp_path / "no-such-universe.yaml")


def test_load_universe_bad_yaml_raises(tmp_path: Path) -> None:
    """YAML that is not a mapping with top-level 'universe' list
    raises ``ValueError``. Two variants:

      * YAML list at the root → 'mapping with a top-level' message.
      * YAML mapping without 'universe' key → 'must contain a' message.

    Both flows must reject at load time, not silently produce
    zero-row ingest at the first daily job.
    """
    p_list = tmp_path / "list-root.yaml"
    p_list.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping with a top-level 'universe' list"):
        load_universe(p_list)

    p_no_key = tmp_path / "no-universe-key.yaml"
    p_no_key.write_text("symbols: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a 'universe' list"):
        load_universe(p_no_key)


def test_load_universe_missing_universe_key_raises(tmp_path: Path) -> None:
    """YAML mapping without 'universe' key raises ``ValueError``."""
    p = tmp_path / "no-key.yaml"
    p.write_text("symbols: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="'universe' list"):
        load_universe(p)


def test_load_universe_empty_list_raises(tmp_path: Path) -> None:
    """Empty ``universe: []`` raises so a misconfigured file does
    not produce a silent zero-row daily ingest."""
    p = tmp_path / "empty.yaml"
    p.write_text("universe: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        load_universe(p)


def test_load_universe_bad_symbol_format_raises(tmp_path: Path) -> None:
    """A symbol that is not 6 digits raises ``ValueError``."""
    p = tmp_path / "bad-symbol.yaml"
    p.write_text(
        "universe:\n  - {symbol: '1', name: 'Bad', sector: 'x'}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="6 digits"):
        load_universe(p)


def test_load_universe_duplicate_symbol_raises(tmp_path: Path) -> None:
    """Duplicate symbol raises ``ValueError`` (silent duplicate
    would double-write on upsert)."""
    p = tmp_path / "dup.yaml"
    p.write_text(
        "universe:\n"
        "  - {symbol: '000001', name: 'A', sector: 'x'}\n"
        "  - {symbol: '000001', name: 'B', sector: 'y'}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate symbol '000001'"):
        load_universe(p)


def test_load_universe_missing_name_or_sector_raises(tmp_path: Path) -> None:
    """Entries missing ``name`` or ``sector`` raise ``ValueError``."""
    p = tmp_path / "missing-fields.yaml"
    p.write_text(
        "universe:\n  - {symbol: '000001', sector: 'x'}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing non-empty 'name'"):
        load_universe(p)


def test_default_path_constant_points_at_existing_file() -> None:
    """The :data:`DEFAULT_UNIVERSE_PATH` sentinel resolves to a
    file that actually exists on disk."""
    assert DEFAULT_UNIVERSE_PATH.exists(), (
        f"DEFAULT_UNIVERSE_PATH does not exist: {DEFAULT_UNIVERSE_PATH}"
    )
    assert DEFAULT_UNIVERSE_PATH.name == "universe.yaml"
