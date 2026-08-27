"""Data layer: ingestion, validation, storage, quality.

Lives at the repo root as a top-level package (alongside ``research``,
``backtest``, ``execution``, ``ops``) rather than inside ``data/``,
because ``data/`` is the on-disk layout for raw / clean parquet,
DuckDB files, and quality reports and is gitignored.

Subpackages:
  * ``ingestion`` — fetcher adapters per source (akshare / adata)
  * ``storage``   — parquet / DuckDB I/O and schema management
  * ``validation``— cross-source diff (akshare vs adata) — placeholder
  * ``quality``   — missing dates / outliers / qfq factor checks — placeholder

The W2 first slice (this commit) ships ``ingestion`` and ``storage``
plus the data_layer/test_data_layer.py integration test. Validation
and quality land in later W2 commits.
"""

from __future__ import annotations
