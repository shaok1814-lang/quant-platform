"""Parquet I/O for daily-bar DataFrames.

Owns the on-disk format for ``data/raw/`` and ``data/clean/``. The
contract is:

* Columns restricted to ``CORE_COLUMNS`` (in stable order) plus any
  optional columns present in the input.
* ``date`` is written as tz-naive ``datetime64[s]`` so DuckDB DATE
  columns can ingest it without an explicit cast.
* Numeric columns are coerced to ``float64`` (akshare occasionally
  returns ``object`` dtype when a row has ``None``; we coerce rather
  than fail).
* ``df.attrs`` round-trips automatically through pyarrow's default
  pandas-metadata behaviour; callers should read it back via
  ``df.attrs['fetcher']`` etc. to verify source-of-truth.

Path layout convention (enforced by callers, not by this module):
``data/raw/{symbol}.parquet`` and ``data/clean/{symbol}.parquet``.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from data_layer.ingestion.akshare_fetcher import CORE_COLUMNS


def write_bars(path: str | Path, df: pd.DataFrame) -> None:
    """Write a daily-bar DataFrame to ``path`` with stable dtypes.

    Raises
    ------
    ValueError
        Any core column is missing from ``df``.
    """
    p = Path(path)
    missing = [c for c in CORE_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"DataFrame missing core columns: {missing}")

    # Restrict to standard columns present in df, in canonical order.
    cols = [c for c in CORE_COLUMNS if c in df.columns]
    out = df.loc[:, cols].copy()

    # Coerce numeric core columns to float64. akshare occasionally
    # returns object dtype when a row has None; to_numeric handles
    # that by inserting NaN instead of raising.
    for c in cols:
        if c == "date":
            continue
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")

    # Drop tz on date if present — DuckDB DATE can't store tz-aware.
    if hasattr(out["date"].dtype, "tz") and out["date"].dtype.tz is not None:
        out["date"] = out["date"].dt.tz_localize(None)

    p.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(out, preserve_index=False)
    pq.write_table(table, p)


def read_bars(path: str | Path) -> pd.DataFrame:
    """Read a daily-bar parquet written by :func:`write_bars`.

    Restores ``df.attrs`` from pyarrow's pandas metadata so callers can
    re-verify ``fetcher`` / ``symbol`` / ``adjust`` / ``fetched_at``.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"parquet not found: {p}")
    return pd.read_parquet(p)
