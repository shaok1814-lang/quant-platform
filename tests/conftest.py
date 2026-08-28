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


__all__ = ["make_bars", "make_multi_symbol_universe"]
