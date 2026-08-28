"""Shared type aliases for the factor library.

Kept in a separate ``_types`` module (underscore prefix signals
"internal but importable") so both ``base`` and the factor modules
can annotate their public APIs without circular imports.
"""

from __future__ import annotations

from typing import TypeAlias

import pandas as pd

# Canonical OHLCV bars DataFrame (i.e. the input shape every factor
# function ultimately consumes). Must satisfy ``CORE_COLUMNS_FACTOR``.
BarsDF: TypeAlias = pd.DataFrame

# Single-factor output. ``name`` is conventionally the factor name
# (e.g. "ma_dev_20"), so pipelines can ``.melt`` directly without
# renaming.
FactorSeries: TypeAlias = pd.Series

# Pipeline long-format output: columns = ["date", "symbol",
# "factor_name", "factor_value"]. Chosen as the default output shape
# because:
#   * DuckDB-friendly PK = (date, symbol, factor_name) for a future
#     factor_values table
#   * cross-symbol ranking is a one-liner with pivot
#   * W5 walk-forward panel datasets do not bloat with N factor
#     columns when the factor list changes
LongFactorDF: TypeAlias = pd.DataFrame
