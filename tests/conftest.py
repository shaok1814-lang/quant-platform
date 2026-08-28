"""Shared test helpers for the W2 / W3 / W3.2 test suites.

Centralizes synthetic OHLCV bars construction so factor / strategy /
data-layer tests can share fixtures without duplicating boilerplate.

These are module-level plain functions (NOT ``@pytest.fixture``) so
callers can import them directly via::

    from tests.conftest import make_bars, make_multi_symbol_universe

Rationale for ``conftest.py`` placement (vs ``tests/_helpers.py``):
``conftest.py`` is auto-loaded by pytest and lives on ``sys.path``
alongside the tests; importing it from any test module works without
extra path manipulation. The drawback — pytest registers all
module-level callables here as fixture candidates — is acceptable
because none of these helpers collide with test parameter names.

History:
  * W2.1 — ``_toy_bars`` lived inline in ``tests/test_data_layer.py``.
  * W2.2 — ``_make_bars`` duplicated in ``tests/test_cross_source.py``.
  * W3.1-C2 — both consolidated here; ``test_cross_source.py``
    refactored to ``from tests.conftest import make_bars as _make_bars``.
"""

from __future__ import annotations

from typing import Final

import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default attrs every synthetic bars frame should carry so the data
# layer's ``df.attrs['symbol']`` validation does not surprise factor
# tests when they read the same frame.
_DEFAULT_ATTRS: Final[dict[str, str]] = {
    "fetcher": "synthetic",
    "symbol": "000001",
    "adjust": "qfq",
    "fetched_at": "2026-08-27T00:00:00+00:00",
}

_DEFAULT_VOLUME: Final[float] = 1_000_000.0
_DEFAULT_AMOUNT: Final[float] = 10_000_000.0
_DEFAULT_OUTSTANDING_SHARE: Final[float] = 1e10
_DEFAULT_HIGH_LOW_SPREAD: Final[float] = 0.05

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def make_bars(
    closes: list[float],
    *,
    fetcher: str = "synthetic",
    symbol: str = "000001",
    start: str = "2024-01-08",
    include_outstanding: bool = False,
) -> pd.DataFrame:
    """Build a canonical-bars DataFrame with the given close series.

    Other OHLCV columns are filled with synthetic-but-deterministic
    values:

      * ``open``   = ``close`` (flat intra-bar, keeps tests order-of-magnitude stable)
      * ``high``   = ``close + 0.05``
      * ``low``    = ``close - 0.05``
      * ``volume`` = 1,000,000
      * ``amount`` = 10,000,000

    ``date`` is a business-day index ending at ``start + (n-1)`` BD so
    the last close matches the most-recent bar.

    Args:
        closes: Close prices, oldest first. Must be non-empty.
        fetcher: Value for ``df.attrs['fetcher']``. Default
            ``"synthetic"`` so cross-source / parity tests can
            distinguish synthetic frames from real fetcher output.
        symbol: Value for ``df.attrs['symbol']``. Default ``"000001"``.
        start: ISO date hint for the LAST bar. The synthetic index
            runs ``bdate_range(end=start, periods=n)``.
        include_outstanding: If True, append an ``outstanding_share``
            column (constant ``1e10``) for ``turnover_ratio`` tests.

    Returns:
        DataFrame satisfying ``CORE_COLUMNS_FACTOR`` (+ optional
        ``outstanding_share``) and carrying the four provenance
        attrs the data layer reads.

    Raises:
        ValueError: if ``closes`` is empty.
    """
    if not closes:
        raise ValueError("closes must be non-empty")
    n = len(closes)
    end = pd.Timestamp(start) + pd.offsets.BDay(n - 1)
    dates = pd.bdate_range(end=end, periods=n)
    df = pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": [c + _DEFAULT_HIGH_LOW_SPREAD for c in closes],
            "low": [c - _DEFAULT_HIGH_LOW_SPREAD for c in closes],
            "close": closes,
            "volume": [_DEFAULT_VOLUME] * n,
            "amount": [_DEFAULT_AMOUNT] * n,
        }
    )
    if include_outstanding:
        df["outstanding_share"] = [_DEFAULT_OUTSTANDING_SHARE] * n
    df.attrs.update(_DEFAULT_ATTRS)
    df.attrs["fetcher"] = fetcher
    df.attrs["symbol"] = symbol
    return df


def make_multi_symbol_universe(
    per_symbol: dict[str, list[float]],
    *,
    fetcher: str = "synthetic",
    start: str = "2024-01-08",
) -> dict[str, pd.DataFrame]:
    """Build a multi-symbol bars universe.

    Returns a ``Dict[str, pd.DataFrame]`` — the shape ``run_backtest``
    accepts for multi-symbol backtests (per AKQuant's
    ``BacktestDataInput`` union).

    Args:
        per_symbol: Mapping ``symbol -> list[float]`` of closes.
        fetcher: Passed through to ``make_bars`` for every frame.
        start: Passed through to ``make_bars`` for every frame.

    Returns:
        ``{symbol: bars_df}``. The ``fetcher`` and ``start`` are
        shared across all symbols so cross-symbol rankings are not
        affected by date-base drift.
    """
    return {
        sym: make_bars(closes, fetcher=fetcher, symbol=sym, start=start)
        for sym, closes in per_symbol.items()
    }


# ---------------------------------------------------------------------------
# W4 OHLCV helpers for A-share rule tests
# ---------------------------------------------------------------------------


def make_limit_up_bars() -> pd.DataFrame:
    """5-bar synthetic OHLCV with bar 2 sitting exactly on the upper limit.

    Bar 0: close=10.00 (normal).
    Bar 1: close=10.50 (normal).
    Bar 2: close=11.00 (= round(10*1.10, 2), main board upper limit).
    Bars 3-4: normal trading at 11.50, 12.00.

    Used by ``tests/test_a_share_rules.py::test_limit_up_blocks_buy``.
    """
    dates = pd.bdate_range(end=pd.Timestamp("2024-01-12"), periods=5)
    closes = [10.00, 10.50, 11.00, 11.50, 12.00]
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": [c + 0.01 for c in closes],
            "low": [c - 0.01 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * 5,
        }
    )


def make_suspension_bars() -> pd.DataFrame:
    """5-bar synthetic OHLCV with bar 2 suspended (volume=0 + flat-line).

    Bar 0: normal close=10.00, volume=1M.
    Bar 1: normal close=10.50, volume=1M.
    Bar 2: close=10.50 (unchanged), volume=0, high=low=10.50 → SUSPENDED.
    Bars 3-4: normal close=11.00, 11.50, volume=1M.

    Used by ``tests/test_a_share_rules.py::test_suspension_no_fill``.
    """
    dates = pd.bdate_range(end=pd.Timestamp("2024-01-12"), periods=5)
    closes = [10.00, 10.50, 10.50, 11.00, 11.50]
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": [10.05, 10.55, 10.50, 11.05, 11.55],
            "low": [9.95, 9.95, 10.50, 9.95, 9.95],
            "close": closes,
            "volume": [1_000_000.0, 1_000_000.0, 0.0, 1_000_000.0, 1_000_000.0],
        }
    )


def make_ex_dividend_bars() -> pd.DataFrame:
    """4-bar synthetic OHLCV with a 5% dividend at bar 2 (qfq-adjusted).

    Bar 0: close_raw=10.00, adj_factor=1.0 → qfq close=10.00.
    Bar 1: close_raw=10.00, adj_factor=1.0 → qfq close=10.00.
    Bar 2: close_raw=10.00, adj_factor=0.95 → qfq close=9.50.
    Bar 3: close_raw=10.00, adj_factor=0.95 → qfq close=9.50.

    Used by ``tests/test_a_share_rules.py::test_ex_dividend_adjustment``.
    The strategy / detector checks the adj_factor jump at bar 2 and
    verifies the close-to-close return * adj_factor ratio is the
    flat-line invariant.
    """
    dates = pd.bdate_range(end=pd.Timestamp("2024-01-11"), periods=4)
    closes = [10.00, 10.00, 9.50, 9.50]
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": [c + 0.01 for c in closes],
            "low": [c - 0.01 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * 4,
            "adj_factor": [1.0, 1.0, 0.95, 0.95],
        }
    )


__all__ = [
    "make_bars",
    "make_ex_dividend_bars",
    "make_limit_up_bars",
    "make_multi_symbol_universe",
    "make_suspension_bars",
]
