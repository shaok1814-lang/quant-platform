"""DuckDB storage layer for daily bars.

Single-table v1 schema: ``daily_bars(symbol, date PRIMARY KEY, ... OHLCV,
provenance columns)``. Provenance columns mirror ``df.attrs`` so any
row can be traced back to its fetcher / adjust / fetch timestamp.

Usage:

    with DuckStore("data/duckdb/daily.duckdb") as store:
        store.upsert_daily_bars(df)
        out = store.query_daily_bars("000001", "2024-09-01", "2026-08-25")

``upsert_daily_bars`` uses DuckDB's ``INSERT ... ON CONFLICT DO UPDATE``
semantics so re-fetching the same window is idempotent: the latest
provenance wins, earlier rows are overwritten in place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import duckdb
import pandas as pd

from data_layer.ingestion.akshare_fetcher import CORE_COLUMNS

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_TABLE_COLUMNS: Final = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "outstanding_share",
    "fetcher",
    "adjust",
    "fetched_at",
)

# ``turnover`` was originally in the schema but akshare 1.18 dropped
# it intermittently and baostock never returned it. v2 schema
# demotes turnover to application-level derivation (volume /
# outstanding_share) so the table stays narrow and stable.
_DDL: Final = """
CREATE TABLE IF NOT EXISTS daily_bars (
    symbol            VARCHAR  NOT NULL,
    date              DATE     NOT NULL,
    open              DOUBLE,
    high              DOUBLE,
    low               DOUBLE,
    close             DOUBLE,
    volume            DOUBLE,
    amount            DOUBLE,
    outstanding_share DOUBLE,
    fetcher           VARCHAR,
    adjust            VARCHAR,
    fetched_at        VARCHAR,
    PRIMARY KEY (symbol, date)
);
"""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class DuckStore:
    """Context-managed DuckDB connection for the daily_bars table.

    The connection is opened on ``__enter__`` and closed on
    ``__exit__``. The schema is created (idempotent) on entry.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: duckdb.DuckDBPyConnection | None = None

    def __enter__(self) -> DuckStore:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(str(self.path))
        self._conn.execute(_DDL)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            raise RuntimeError("DuckStore used outside 'with' block")
        return self._conn

    # -----------------------------------------------------------------------
    # Writes
    # -----------------------------------------------------------------------

    def upsert_daily_bars(self, df: pd.DataFrame) -> int:
        """Upsert rows from ``df`` into ``daily_bars``.

        Provenance (``fetcher`` / ``adjust`` / ``fetched_at`` /
        ``symbol``) is read from ``df.attrs`` so the same DataFrame
        that came out of ``fetch_daily_bars`` round-trips cleanly.

        Returns the number of rows passed in (DuckDB's ``rowcount`` on
        INSERT is not always reliable across versions, so we report
        ``len(df)`` when rowcount is unavailable).
        """
        if df.empty:
            return 0

        symbol = str(df.attrs.get("symbol", ""))
        fetcher = str(df.attrs.get("fetcher", ""))
        adjust = str(df.attrs.get("adjust", ""))
        fetched_at = str(df.attrs.get("fetched_at", ""))
        if not symbol:
            raise ValueError("df.attrs['symbol'] is required for upsert")

        # Register DataFrame as a view for the duration of this call.
        # ``unregister`` is best-effort — DuckDB drops the view on
        # connection close anyway.
        self.conn.register("df_view", df)

        # Optional columns may be absent in df; substitute NULL.
        optional_cols = {"outstanding_share"}
        present_optional = ", ".join(
            f"df_view.{c} AS {c}"
            for c in optional_cols
            if c in df.columns
        ) or "NULL::DOUBLE AS outstanding_share"

        core_select = ", ".join(f"df_view.{c}" for c in CORE_COLUMNS)

        sql = f"""
            INSERT INTO daily_bars ({", ".join(_TABLE_COLUMNS)})
            SELECT
                CAST(? AS VARCHAR) AS symbol,
                {core_select},
                {present_optional},
                CAST(? AS VARCHAR) AS fetcher,
                CAST(? AS VARCHAR) AS adjust,
                CAST(? AS VARCHAR) AS fetched_at
            FROM df_view
            ON CONFLICT (symbol, date) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume,
                amount = excluded.amount,
                outstanding_share = excluded.outstanding_share,
                fetcher = excluded.fetcher,
                adjust = excluded.adjust,
                fetched_at = excluded.fetched_at
        """
        try:
            cur = self.conn.execute(
                sql, [symbol, fetcher, adjust, fetched_at]
            )
            rowcount = cur.rowcount
        finally:
            self.conn.unregister("df_view")
        return rowcount if rowcount is not None and rowcount >= 0 else len(df)

    # -----------------------------------------------------------------------
    # Reads
    # -----------------------------------------------------------------------

    def query_daily_bars(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        """Return rows for ``symbol`` between ``start_date`` and ``end_date``.

        Date bounds are inclusive on both ends and use ``YYYY-MM-DD``
        string format. Either bound may be ``None`` (open-ended).
        """
        clauses = ["symbol = ?"]
        params: list[str] = [symbol]
        if start_date is not None:
            clauses.append("date >= CAST(? AS DATE)")
            params.append(start_date)
        if end_date is not None:
            clauses.append("date <= CAST(? AS DATE)")
            params.append(end_date)

        sql = f"""
            SELECT {", ".join(_TABLE_COLUMNS)}
            FROM daily_bars
            WHERE {" AND ".join(clauses)}
            ORDER BY date
        """
        return self.conn.execute(sql, params).df()
