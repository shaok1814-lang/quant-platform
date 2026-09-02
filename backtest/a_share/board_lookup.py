"""Board lookup by 6-digit A-share code prefix (W4 rule layer).

Pure-function helper used by the runner's price-limit guard when
``session_cfg.board_map`` is ``None``. Covers 99% of A-share
universe via code-prefix conventions:

  * ``6xxxxx`` Shanghai main (also ETFs ``5xxxxx``)
  * ``688xxx`` Shanghai STAR Market
  * ``0xxxxx``, ``3xxxxx`` Shenzhen main + SMEB
  * ``300xxx``, ``301xxx`` Shenzhen ChiNext
  * ``8xxxxx``, ``4xxxxx``, ``92xxxx`` Beijing Stock Exchange

Not derivable from the code:
  * ETFs (cross-exchange, multi-board)
  * BJS sub-board divisions
  * ST status (separate ``fetch_st_symbols`` snapshot)

For ambiguous symbols, callers should pass an explicit
``board_map`` via :class:`PaperSessionConfig`.
"""
from __future__ import annotations

from backtest.a_share._types import Board

__all__ = ["board_for_symbol"]


def board_for_symbol(symbol: str) -> Board:
    """Derive exchange board from 6-digit code prefix.

    Args:
        symbol: 6-digit A-share code (e.g. ``"000001"``).

    Returns:
        One of ``"main"`` / ``"chinext"`` / ``"star"`` / ``"bjs"``.

    Raises:
        ValueError: if ``symbol`` is not 6 digits or no prefix
            matches a known board.
    """
    if not (isinstance(symbol, str) and len(symbol) == 6 and symbol.isdigit()):
        raise ValueError(
            f"symbol must be 6 digits, got {symbol!r}; (akshare fetch_daily_bars contract)"
        )

    # STAR Market (Shanghai) — 688xxx
    if symbol.startswith("688"):
        return "star"

    # Shenzhen main + SMEB (中小板) — 000xxx / 001xxx / 002xxx / 003xxx
    if symbol.startswith(("000", "001", "002", "003")):
        return "main"

    # ChiNext (Shenzhen) — 300xxx / 301xxx
    if symbol.startswith(("300", "301")):
        return "chinext"

    # Shanghai main (主板 + ETF + 科创板非 688) — 6xxxxx / 5xxxxx
    if symbol.startswith(("600", "601", "603", "605")):
        return "main"

    # Beijing Stock Exchange — 8xxxxx / 4xxxxx / 92xxxx
    if symbol.startswith(("8", "4", "92")):
        return "bjs"

    raise ValueError(
        f"unknown A-share board for symbol {symbol!r}; "
        f"pass an explicit board_map via PaperSessionConfig"
    )
