"""ST-flagged symbol filter (CLAUDE.md: ST 默认过滤).

akshare exposes a point-in-time snapshot of the ST risk-warning board
via ``ak.stock_zh_a_st_em()``. There is **no historical ST membership
series** in akshare — the snapshot is "what is ST today". W4 ships
the canonical function and an offline CSV fallback so a strategy
that does not have network access (CI / sandboxed runs) can still
filter by a frozen snapshot.

Boundary semantics:

  * ``filter_st(include_st=False)`` (default per CLAUDE.md) drops
    any symbol in ``st_set``. ``include_st=True`` keeps them.
  * ``fetch_st_symbols(allow_network=False)`` reads the offline CSV
    only; network failure is not a raise, just an empty result.
  * Code columns are normalized to 6-digit zero-padded strings
    regardless of source (akshare returns strings; CSV may have int).
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

# Default offline CSV location. Path is relative to the project root;
# CI / sandboxed runs can populate this once and skip the network.
OFFLINE_ST_CSV: Final[Path] = Path("data/st_a_share_list.csv")

# The akshare column that carries the symbol code in
# ``stock_zh_a_st_em()``'s output. Verified at the W4 survey.
_AKSHARE_ST_CODE_COL: Final[str] = "代码"

__all__ = [
    "OFFLINE_ST_CSV",
    "fetch_st_symbols",
    "filter_st",
]


def _normalize_code(raw: object) -> str | None:
    """Coerce a symbol-code cell to a 6-digit zero-padded string.

    Returns ``None`` for unparseable cells so the caller can skip
    them rather than raise.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # If it's already a 6-digit string, return as-is.
    if s.isdigit() and len(s) <= 6:
        return s.zfill(6)
    return s  # pass-through; let the caller decide what to do


def _read_offline_csv(path: Path) -> set[str]:
    """Read the offline CSV at ``path`` and return the symbol set.

    Expects a single column whose header is the symbol-code column
    name (defaults to ``代码`` — the akshare convention). If the
    file does not exist, returns an empty set (logged at WARNING).
    """
    if not path.exists():
        logger.warning(
            "ST offline CSV missing at %s; ST filter will return empty set",
            path,
        )
        return set()
    out: set[str] = set()
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or _AKSHARE_ST_CODE_COL not in reader.fieldnames:
            logger.warning(
                "ST offline CSV at %s is missing column %r; ST filter will return empty set",
                path,
                _AKSHARE_ST_CODE_COL,
            )
            return set()
        for row in reader:
            normalized = _normalize_code(row.get(_AKSHARE_ST_CODE_COL))
            if normalized is not None:
                out.add(normalized)
    return out


def _fetch_via_network() -> set[str]:
    """Wrap ``ak.stock_zh_a_st_em()`` and normalize.

    Raises any exception the caller wishes to handle (wrapped
    ``fetch_st_symbols`` logs and returns ``set()`` on network
    failure unless ``allow_network=False`` is overridden).
    """
    import akshare as ak  # local import; ak not needed for offline mode

    df = ak.stock_zh_a_st_em()
    if df is None or df.empty:
        return set()
    out: set[str] = set()
    for raw in df[_AKSHARE_ST_CODE_COL]:
        normalized = _normalize_code(raw)
        if normalized is not None:
            out.add(normalized)
    return out


def fetch_st_symbols(
    *,
    offline_csv: Path | None = OFFLINE_ST_CSV,
    allow_network: bool = True,
) -> set[str]:
    """Return the set of currently-ST-flagged A-share codes.

    Args:
        offline_csv: Path to a CSV snapshot. If ``None``, skip the
            offline path (pure network call). Default points at
            ``data/st_a_share_list.csv`` relative to project root.
        allow_network: If ``False``, skip the akshare call entirely
            (CI / sandboxed runs). Default ``True``.

    Returns:
        ``set[str]`` of 6-digit normalized codes. Empty if both
        paths return nothing (e.g. offline CSV missing + network
        blocked).
    """
    out: set[str] = set()
    if offline_csv is not None:
        out |= _read_offline_csv(offline_csv)
    if allow_network:
        try:
            out |= _fetch_via_network()
        except Exception as exc:
            logger.warning(
                "akshare fetch failed for ST list (%s); network result ignored",
                exc,
            )
    return out


def filter_st(
    symbols: list[str],
    *,
    include_st: bool = False,
    st_set: set[str] | None = None,
) -> list[str]:
    """Filter ST symbols from a universe.

    Args:
        symbols: Universe to filter. Order is preserved.
        include_st: ``True`` keeps ST symbols (opt-in). Default
            ``False`` drops them per CLAUDE.md "ST 股票默认过滤".
        st_set: Pre-fetched ST set. If ``None``, ``fetch_st_symbols``
            is called (network + offline CSV). Tests inject a
            pre-built set to avoid network.

    Returns:
        Filtered list in the original order.
    """
    st = st_set if st_set is not None else fetch_st_symbols()
    if include_st:
        return list(symbols)
    return [s for s in symbols if s not in st]
