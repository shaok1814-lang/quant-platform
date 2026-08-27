"""akshare fetcher — primary A-share daily K-line source.

Wraps ``ak.stock_zh_a_hist`` with:

* Strict input validation (symbol must be 6 digits; ``adjust`` in
  ``{"qfq", "hfq", ""}``).
* Explicit error when akshare returns empty (network / delisted /
  trading-halt window).
* Renamed / column-ordered DataFrame to a stable schema used by
  storage + validation + backtest.
* ``df.attrs`` provenance (``fetcher`` / ``adjust`` / ``fetched_at`` /
  ``symbol``) so downstream layers can trace any row back to source.

降级到 adata 在 W2.2（cross-source validation）实装；本文件不假设
adata 在线。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final, Literal

import akshare as ak
import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADJUST_QFQ: Final = "qfq"
ADJUST_HFQ: Final = "hfq"
ADJUST_NONE: Final = ""
ADJUST_CHOICES: Final = (ADJUST_QFQ, ADJUST_HFQ, ADJUST_NONE)

AdjustMode = Literal["qfq", "hfq", ""]

# Stable column order used by storage / validation / backtest. Any new
# column lands at the end with a backwards-compatible default.
#
# Two-tier schema:
#   CORE_COLUMNS     — akshare ``stock_zh_a_hist`` is contracted to
#                      return these; missing any of them is a FetcherError.
#   OPTIONAL_COLUMNS — fields that may not be present in every akshare
#                      release (e.g. ``outstanding_share`` was dropped in
#                      recent versions and now requires a separate
#                      ``stock_individual_info_em`` call). If present
#                      they are kept; otherwise silently skipped.
CORE_COLUMNS: Final = (
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "turnover",
)

OPTIONAL_COLUMNS: Final = ("outstanding_share",)

STANDARD_COLUMNS: Final = CORE_COLUMNS + OPTIONAL_COLUMNS

# akshare's stock_zh_a_hist uses Chinese column names; map them to
# the English schema so the rest of the stack never has to handle
# akshare's locale drift.
_AKSHARE_COLUMN_MAP: Final[dict[str, str]] = {
    "日期": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
    "换手率": "turnover",
    "总股本": "outstanding_share",
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FetcherError(RuntimeError):
    """Raised when akshare returns no usable data.

    Caller is expected to either retry, fall back to adata (W2.2), or
    surface the gap to the user. ``FetcherError`` is a ``RuntimeError``
    so existing try/except RuntimeError blocks in research code keep
    working unchanged.
    """


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_daily_bars(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    adjust: AdjustMode = ADJUST_QFQ,
) -> pd.DataFrame:
    """Fetch daily OHLCV bars for ``symbol`` between ``start_date`` and ``end_date``.

    Parameters
    ----------
    symbol : str
        6-digit A-share symbol without exchange suffix (e.g. ``"000001"``).
    start_date, end_date : str
        ``YYYYMMDD``. Inclusive on both ends.
    adjust : {"qfq", "hfq", ""}
        前复权 / 后复权 / 不复权. Default 前复权 per CLAUDE.md.

    Returns
    -------
    pd.DataFrame
        Columns in ``STANDARD_COLUMNS`` order. ``df.attrs`` carries
        ``fetcher`` / ``symbol`` / ``adjust`` / ``fetched_at`` (UTC ISO).

    Raises
    ------
    ValueError
        Bad symbol or adjust mode.
    FetcherError
        akshare returned ``None`` or empty ``DataFrame`` (network /
        delisted / suspended window).
    """
    if not symbol.isdigit() or len(symbol) != 6:
        raise ValueError(f"symbol must be 6 digits, got {symbol!r}")
    if adjust not in ADJUST_CHOICES:
        raise ValueError(
            f"adjust must be one of {ADJUST_CHOICES!r}, got {adjust!r}"
        )

    raw = ak.stock_zh_a_hist(
        symbol=symbol,
        period="daily",
        start_date=start_date,
        end_date=end_date,
        adjust=adjust,
    )
    if raw is None or raw.empty:
        raise FetcherError(
            f"akshare returned no rows for {symbol} between "
            f"{start_date} and {end_date}; check network or symbol"
        )

    df = raw.rename(columns=_AKSHARE_COLUMN_MAP)
    missing_core = [c for c in CORE_COLUMNS if c not in df.columns]
    if missing_core:
        raise FetcherError(
            f"akshare response missing core columns {missing_core}; "
            f"got {list(df.columns)}"
        )

    # Keep core + whichever optionals happen to be present, in stable
    # column order. Skipping absent optionals keeps fetcher stable across
    # akshare releases that drop / add fields.
    present = [c for c in STANDARD_COLUMNS if c in df.columns]

    df = df.loc[:, present].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)

    # Provenance. Set last — earlier ops may strip ``attrs`` on copy().
    df.attrs["fetcher"] = "akshare"
    df.attrs["symbol"] = symbol
    df.attrs["adjust"] = adjust
    df.attrs["fetched_at"] = datetime.now(UTC).isoformat()

    return df
