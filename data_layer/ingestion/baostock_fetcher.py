"""baostock fetcher — secondary A-share daily K source.

Used by W2.2 as the cross-source validator against akshare. baostock
is free, no token required, and routes through its own HTTP stack
(no Windows system-proxy issues). Trade-off: data is delayed by ~15
minutes and there's no realtime quote path — fine for daily-bar
validation, not for live trading.

Public API mirrors :func:`data_layer.ingestion.akshare_fetcher.fetch_daily_bars`
so callers can swap sources without rewriting call sites. The two
fetchers do *not* share a base class; they only share the column
contract (``CORE_COLUMNS`` from akshare_fetcher) and ``df.attrs``
provenance keys.

Date format: this module accepts ``YYYY-MM-DD`` (ISO). The baostock
SDK uses the same format natively; no internal conversion needed.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Final, Literal

import baostock as bs
import pandas as pd

from data_layer.ingestion.akshare_fetcher import CORE_COLUMNS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# baostock adjustflag: 1=后复权, 2=前复权, 3=不复权. The mapping below
# mirrors the akshare adjust semantics so callers can pass the same
# string to either fetcher.
ADJUST_BFQ: Final = "1"  # 后复权
ADJUST_QFQ: Final = "2"  # 前复权
ADJUST_NONE: Final = "3"  # 不复权
ADJUST_CHOICES: Final = (ADJUST_BFQ, ADJUST_QFQ, ADJUST_NONE)

_AKSHARE_TO_BAOSTOCK: Final[dict[str, str]] = {
    "qfq": ADJUST_QFQ,
    "hfq": ADJUST_BFQ,
    "": ADJUST_NONE,
}

# 6xxxxx / 9xxxxx trade on Shanghai; everything else on Shenzhen.
# Heuristic matches baostock's own prefix rules.
_SHANGHAI_PREFIXES: Final = ("60", "68", "90", "11", "13")

_DATE_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")

AdjustMode = Literal["qfq", "hfq", ""]

# baostock field order for query_history_k_data_plus. Note the
# trailing "amount" is the same as akshare's "成交额".
_BAOSTOCK_FIELDS: Final = "date,open,high,low,close,volume,amount"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FetcherError(RuntimeError):
    """Raised when baostock returns no usable data or login fails.

    Mirrors :class:`data_layer.ingestion.akshare_fetcher.FetcherError`
    so callers can catch both with a single ``except FetcherError``.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _code_with_exchange(symbol: str) -> str:
    """Prefix ``symbol`` with ``sh.`` or ``sz.`` per baostock convention.

    Examples
    --------
    >>> _code_with_exchange("000001")
    'sz.000001'
    >>> _code_with_exchange("600000")
    'sh.600000'
    """
    if not symbol.isdigit() or len(symbol) != 6:
        raise ValueError(f"symbol must be 6 digits, got {symbol!r}")
    if symbol.startswith(_SHANGHAI_PREFIXES):
        return f"sh.{symbol}"
    return f"sz.{symbol}"


def _check_date(s: str) -> str:
    if not isinstance(s, str) or not _DATE_RE.match(s):
        raise ValueError(f"date must be YYYY-MM-DD, got {s!r}")
    return s


@contextmanager
def _baostock_session() -> Iterator[None]:
    """Open / close a baostock session.

    baostock holds an HTTP connection per process; explicit login /
    logout keeps tests hermetic and lets the fetcher run multiple
    times without leaking sessions.
    """
    lg = bs.login()
    if lg.error_code != "0":
        raise FetcherError(f"baostock login failed: {lg.error_msg}")
    try:
        yield
    finally:
        bs.logout()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_daily_bars(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    adjust: AdjustMode = "qfq",
) -> pd.DataFrame:
    """Fetch daily OHLCV bars for ``symbol`` via baostock.

    Parameters
    ----------
    symbol : str
        6-digit A-share symbol without exchange suffix (``"000001"``).
    start_date, end_date : str
        ``YYYY-MM-DD`` (ISO). Inclusive on both ends.
    adjust : {"qfq", "hfq", ""}
        Mirror of the akshare adjust semantics.

    Returns
    -------
    pd.DataFrame
        Same CORE_COLUMNS contract as
        :func:`akshare_fetcher.fetch_daily_bars`. ``df.attrs`` carries
        ``fetcher`` / ``symbol`` / ``adjust`` / ``fetched_at``.
    """
    if adjust not in _AKSHARE_TO_BAOSTOCK:
        raise ValueError(
            f"adjust must be one of {tuple(_AKSHARE_TO_BAOSTOCK)!r}, "
            f"got {adjust!r}"
        )
    _check_date(start_date)
    _check_date(end_date)

    code = _code_with_exchange(symbol)
    adjust_flag = _AKSHARE_TO_BAOSTOCK[adjust]

    with _baostock_session():
        rs = bs.query_history_k_data_plus(
            code,
            _BAOSTOCK_FIELDS,
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag=adjust_flag,
        )
        if rs.error_code != "0":
            raise FetcherError(
                f"baostock query failed for {code}: {rs.error_msg}"
            )

        rows: list[list[str]] = []
        while rs.next():
            rows.append(rs.get_row_data())

    if not rows:
        raise FetcherError(
            f"baostock returned no rows for {code} between "
            f"{start_date} and {end_date}"
        )

    df = pd.DataFrame(rows, columns=rs.fields)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    for c in ("open", "high", "low", "close", "volume", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")

    # baostock doesn't return outstanding_share / turnover (both are
    # OPTIONAL_COLUMNS in the schema). Storage layer skips absent
    # optionals, so we just project to whatever CORE+OPTIONAL are
    # actually present.
    present = [c for c in (*CORE_COLUMNS, "turnover", "outstanding_share") if c in df.columns]
    df = df.loc[:, present].copy()

    # Provenance.
    df.attrs["fetcher"] = "baostock"
    df.attrs["symbol"] = symbol
    df.attrs["adjust"] = adjust
    df.attrs["fetched_at"] = datetime.now(UTC).isoformat()

    return df
