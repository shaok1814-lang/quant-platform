"""Universe loader (W6.1).

Reads ``config/universe.yaml`` and returns a validated
``list[UniverseEntry]`` so ``ops.ingest_job`` knows which symbols
to fetch daily.

Validation rules (CLAUDE.md "数据可靠" + W6.1 do-not-silently-fail):

  * ``symbol`` MUST be 6 digits (akshare ``fetch_daily_bars``
    contract — it raises ``ValueError`` otherwise).
  * Symbols MUST be unique within the file (silent duplicates
    would double-write the same PK on upsert and could mask
    misconfigurations).
  * Each entry MUST have ``symbol`` + ``name`` + ``sector``. Missing
    keys raise so a typo in YAML is caught at scheduler start,
    not on the first daily ingest at 18:00.

The default path resolves to ``<project-root>/config/universe.yaml``
via :data:`DEFAULT_UNIVERSE_PATH`. Tests inject a different path
via the ``path`` kwarg; production callers (the scheduler) can rely
on the default.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml
from loguru import logger

__all__ = ["DEFAULT_UNIVERSE_PATH", "UniverseEntry", "load_universe"]

# Project root is ops/'s parent's parent. ops/ sits alongside
# research/ / data_layer/ / config/ etc. We resolve up to ensure
# the default path is stable regardless of cwd.
_PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_UNIVERSE_PATH: Final[Path] = _PROJECT_ROOT / "config" / "universe.yaml"


@dataclass(frozen=True)
class UniverseEntry:
    """One row of the universe YAML.

    Attributes:
        symbol: 6-digit A-share symbol (e.g. ``"000001"``).
        name: Human-readable name (e.g. ``"Ping An Bank"``).
        sector: Free-form sector tag for grouped diagnostics.
            Examples: ``"bank"``, ``"etf"``, ``"new_energy"``.
    """

    symbol: str
    name: str
    sector: str


def _validate_symbol(symbol: object) -> str:
    """Coerce + validate a symbol entry.

    Raises:
        ValueError: if symbol is not a 6-digit string.
    """
    if not isinstance(symbol, str):
        raise ValueError(f"symbol must be a 6-digit string, got {type(symbol).__name__}")
    if not (symbol.isdigit() and len(symbol) == 6):
        raise ValueError(
            f"symbol must be 6 digits, got {symbol!r}; (akshare fetch_daily_bars contract)"
        )
    return symbol


def load_universe(path: str | Path | None = None) -> list[UniverseEntry]:
    """Load + validate ``universe.yaml``.

    Args:
        path: Path to the YAML file. Defaults to
            :data:`DEFAULT_UNIVERSE_PATH`. Pass a non-default path
            in tests.

    Returns:
        Sorted (by symbol) list of :class:`UniverseEntry`. Sorting
        is deterministic so the scheduler's per-symbol ingest order
        is reproducible across runs.

    Raises:
        FileNotFoundError: if ``path`` does not exist.
        ValueError: on any schema / validation failure (missing
            keys, bad symbol, duplicate symbol, wrong root key,
            wrong entry type).
        yaml.YAMLError: if the file content is not valid YAML.
    """
    universe_path = Path(path) if path is not None else DEFAULT_UNIVERSE_PATH
    if not universe_path.exists():
        raise FileNotFoundError(f"universe file not found: {universe_path}")

    with universe_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(
            f"universe file must be a YAML mapping with a top-level "
            f"'universe' list, got {type(raw).__name__}"
        )
    rows = raw.get("universe")
    if not isinstance(rows, list):
        raise ValueError(f"universe file must contain a 'universe' list, got {type(rows).__name__}")
    if not rows:
        raise ValueError("universe list is empty; nothing to ingest")

    entries: list[UniverseEntry] = []
    seen: set[str] = set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(
                f"universe[{i}] must be a mapping with symbol/name/sector, got {type(row).__name__}"
            )
        symbol = _validate_symbol(row.get("symbol"))
        name = row.get("name")
        sector = row.get("sector")
        if not isinstance(name, str) or not name:
            raise ValueError(f"universe[{i}] (symbol={symbol!r}) missing non-empty 'name'")
        if not isinstance(sector, str) or not sector:
            raise ValueError(f"universe[{i}] (symbol={symbol!r}) missing non-empty 'sector'")
        if symbol in seen:
            raise ValueError(
                f"duplicate symbol {symbol!r} in universe file "
                f"(silent duplicates would double-write on upsert)"
            )
        seen.add(symbol)
        entries.append(UniverseEntry(symbol=symbol, name=name, sector=sector))

    entries.sort(key=lambda e: e.symbol)
    logger.info(
        "loaded universe: {n} symbols across {s} sectors",
        n=len(entries),
        s=len({e.sector for e in entries}),
    )
    return entries


def load_filtered_universe(
    path: str | Path | None = None,
    *,
    include_st: bool = False,
    include_delisted: bool = True,
) -> list[UniverseEntry]:
    """Load universe YAML and apply CLAUDE.md A-share rules.

    Wrapper around :func:`load_universe` that applies the two
    universe-layer rules from CLAUDE.md:

      * **ST 股票过滤** (``filter_st``) — default drops ST symbols.
      * **幸存者偏差** (``build_universe(include_delisted=True)``) —
        default appends delisted symbols so a backtest sample
        includes them.

    Active symbols come from the YAML (with their proper
    ``name`` / ``sector``). Delisted symbols are appended with a
    placeholder ``name="[delisted]"`` and ``sector="delisted"`` so
    IngestReport output identifies them clearly. ST symbols
    returned by ``fetch_st_symbols`` are dropped.

    Both ST and delisted snapshots are read offline-first
    (``allow_network=False``) to keep this deterministic and
    CI-safe. Run ``scripts/snapshot_st_delisted.py`` periodically
    to refresh the snapshots.

    Args:
        path: Optional override of the universe YAML path.
            ``None`` uses :data:`DEFAULT_UNIVERSE_PATH`.
        include_st: Pass-through to :func:`filter_st`. Default
            ``False`` (CLAUDE.md "默认过滤 ST").
        include_delisted: Pass-through to
            :func:`build_universe`. Default ``True`` (CLAUDE.md
            "回测样本必须包含已退市股票").

    Returns:
        A list of :class:`UniverseEntry` objects sorted by symbol.
        Length may differ from the YAML if ST filtering drops any
        active symbols or if delisted inclusion adds more.

    Note:
        The current universe YAML is ST-clean by curation
        (``screened by the operator``), so ST filtering is a
        safety net rather than active suppression. Delisted
        inclusion is the active rule — adding 200-400 symbols
        per snapshot.
    """
    # Lazy imports to avoid pulling akshare at module import time
    # (the universe loader is imported early by ops/__main__.py).
    # Module-qualified access (rather than ``from x import y``)
    # so tests can monkeypatch ``backtest.a_share.fetch_*``.
    from backtest import a_share as _a_share

    active_entries = load_universe(path)
    active_symbols = [e.symbol for e in active_entries]
    st_set = _a_share.fetch_st_symbols(allow_network=False)
    delisted_set = _a_share.fetch_delisted_symbols(allow_network=False)

    combined = _a_share.build_universe(
        active_symbols, include_delisted=include_delisted, delisted_set=delisted_set
    )
    kept = _a_share.filter_st(combined, include_st=include_st, st_set=st_set)

    active_map = {e.symbol: e for e in active_entries}
    out: list[UniverseEntry] = []
    n_active_kept = 0
    n_delisted_added = 0
    for sym in kept:
        if sym in active_map:
            out.append(active_map[sym])
            n_active_kept += 1
        else:
            out.append(UniverseEntry(symbol=sym, name="[delisted]", sector="delisted"))
            n_delisted_added += 1
    out.sort(key=lambda e: e.symbol)

    logger.info(
        "filtered universe: {kept} kept ({act} active + {dl} delisted); "
        "dropped {drop} ST symbols; {st_total} ST total in snapshot, {dl_total} delisted total",
        kept=len(out),
        act=n_active_kept,
        dl=n_delisted_added,
        drop=len(combined) - len(kept),
        st_total=len(st_set),
        dl_total=len(delisted_set),
    )
    return out
