"""Delisted-symbol universe builder (CLAUDE.md: 幸存者偏差).

akshare exposes three endpoints that together cover the A-share
delisted universe:

  * ``ak.stock_info_sh_delist(symbol="全部")`` — SSE terminated companies.
  * ``ak.stock_info_sz_delist(symbol="终止上市公司")`` — SZSE terminated
    (and ``symbol="暂停上市公司"`` for suspended; W4 only needs
    terminated for survivor-bias coverage).
  * ``ak.stock_staq_net_stop()`` — STAQ/NET delisted (老三板),
    via the same ``_em`` variant ``stock_zh_a_stop_em()``.

W4 ships a single function that combines all three, deduplicates by
symbol code, and offers an offline CSV fallback for CI / sandboxed
runs.

Boundary semantics:

  * ``build_universe(active, include_delisted=True)`` (per CLAUDE.md
    "回测样本必须包含已退市股票") returns active symbols PLUS the
    injected delisted set. ``include_delisted=False`` returns active
    only (this is the survivor-biased mode that W4 explicitly
    warns against in production strategies).
  * Offline CSV column header: ``代码`` (akshare convention).
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Final

from backtest.a_share.st_filter import _normalize_code

logger = logging.getLogger(__name__)

# Default offline CSV location for the delisted universe.
OFFLINE_DELISTED_CSV: Final[Path] = Path("data/delisted_a_share_list.csv")

# Source columns per akshare endpoint (verified at W4 survey):
#   * SSE: 公司代码
#   * SZSE: 证券代码
#   * STAQ/NET (em variant): 代码
_AKSHARE_DELISTED_CODE_COLS: Final[tuple[str, ...]] = (
    "代码",
    "公司代码",
    "证券代码",
)


__all__ = [
    "OFFLINE_DELISTED_CSV",
    "build_universe",
    "fetch_delisted_symbols",
]


def _read_offline_csv(path: Path) -> set[str]:
    """Read the offline CSV at ``path`` and return the symbol set.

    Accepts any of the akshare code-column names so a single CSV
    populated from any source (SSE / SZSE / STAQ/NET) works.
    Returns ``set()`` if the file is missing or has no recognizable
    column.
    """
    if not path.exists():
        logger.warning(
            "Delisted offline CSV missing at %s; delisted filter will return empty set",
            path,
        )
        return set()
    out: set[str] = set()
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        cols = set(reader.fieldnames or ())
        code_col = next(
            (c for c in _AKSHARE_DELISTED_CODE_COLS if c in cols),
            None,
        )
        if code_col is None:
            logger.warning(
                "Delisted offline CSV at %s has no recognizable code column; expected one of %s",
                path,
                list(_AKSHARE_DELISTED_CODE_COLS),
            )
            return set()
        for row in reader:
            normalized = _normalize_code(row.get(code_col))
            if normalized is not None:
                out.add(normalized)
    return out


def _fetch_via_network() -> set[str]:
    """Combine SSE + SZSE + STAQ/NET and dedup."""
    import akshare as ak  # local import; not needed for offline mode

    out: set[str] = set()
    # SSE
    try:
        df_sh = ak.stock_info_sh_delist(symbol="全部")
        if df_sh is not None and not df_sh.empty:
            for raw in df_sh["公司代码"]:
                n = _normalize_code(raw)
                if n is not None:
                    out.add(n)
    except Exception as exc:
        logger.warning("akshare SSE delist fetch failed: %s", exc)
    # SZSE (terminated)
    try:
        df_sz = ak.stock_info_sz_delist(symbol="终止上市公司")
        if df_sz is not None and not df_sz.empty:
            for raw in df_sz["证券代码"]:
                n = _normalize_code(raw)
                if n is not None:
                    out.add(n)
    except Exception as exc:
        logger.warning("akshare SZSE delist fetch failed: %s", exc)
    # STAQ/NET
    try:
        df_otc = ak.stock_staq_net_stop()
        if df_otc is not None and not df_otc.empty:
            for raw in df_otc["代码"]:
                n = _normalize_code(raw)
                if n is not None:
                    out.add(n)
    except Exception as exc:
        logger.warning("akshare STAQ/NET delist fetch failed: %s", exc)
    return out


def fetch_delisted_symbols(
    *,
    offline_csv: Path | None = OFFLINE_DELISTED_CSV,
    allow_network: bool = True,
) -> set[str]:
    """Return the dedup'd set of A-share delisted codes.

    Args:
        offline_csv: Path to a CSV snapshot. If ``None``, skip the
            offline path. Default points at
            ``data/delisted_a_share_list.csv`` relative to project root.
        allow_network: If ``False``, skip akshare (CI / sandboxed).

    Returns:
        ``set[str]`` of 6-digit normalized codes.
    """
    out: set[str] = set()
    if offline_csv is not None:
        out |= _read_offline_csv(offline_csv)
    if allow_network:
        out |= _fetch_via_network()
    return out


def build_universe(
    active: list[str],
    *,
    include_delisted: bool = True,
    delisted_set: set[str] | None = None,
) -> list[str]:
    """Build a backtest universe, optionally including delisted symbols.

    Args:
        active: Currently-active symbols (the universe the data
            layer has bars for today).
        include_delisted: If ``True`` (default per CLAUDE.md), append
            the delisted set. ``False`` produces a survivor-biased
            universe and is provided only for legacy /
            experimentation scenarios.
        delisted_set: Pre-fetched delisted set. If ``None``,
            ``fetch_delisted_symbols`` is called (network + offline
            CSV). Tests inject a pre-built set to avoid network.

    Returns:
        Ordered list. Active symbols come first, then delisted
        symbols in their natural (set-iteration) order. Duplicates
        are removed.
    """
    seen: set[str] = set()
    out: list[str] = []
    for s in active:
        if s not in seen:
            seen.add(s)
            out.append(s)
    if include_delisted:
        delisted = delisted_set if delisted_set is not None else fetch_delisted_symbols()
        for s in delisted:
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out
