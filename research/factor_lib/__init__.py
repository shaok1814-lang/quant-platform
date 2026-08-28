"""Self-research factor library (P2 W3).

W3 ships four base factors that cover the four alpha families
``CLAUDE.md`` considers essential for A-share research:

  * trend          — ``ma_deviation``
  * momentum       — ``n_day_return``
  * mean-reversion — ``rsi``, ``bollinger_z``
  * liquidity      — ``turnover_ratio``

Plus three cross-section post-processors:

  * ``winsorize``    — 去极值 (3-sigma / MAD / quantile)
  * ``standardize``  — z-score 标准化
  * ``Neutralizer`` Protocol + ``PassThroughNeutralizer`` —
    中性化钩子 (industry neutralization data lands in W5)

Plus a ``FactorPipeline`` that composes N factors + post-processing
into a single ``compute(df)`` call returning either wide or long
format.

Submodules:
  * ``base``           — CORE_COLUMNS_FACTOR + ``validate_input_bars``
  * ``trend``          — ``ma_deviation``
  * ``momentum``       — ``n_day_return``
  * ``mean_reversion`` — ``rsi``, ``bollinger_z``
  * ``liquidity``      — ``turnover_ratio``
  * ``post``           — winsorize / standardize / neutralizer
  * ``splits``         — train/test split helpers (W5 stub for
                          walk-forward; raises NotImplementedError on
                          misuse to enforce anti-overfit rules)
  * ``pipeline``       — ``FactorPipeline``

The library is **pandas-only** — AKQuant's polars-based factor DSL
(``akquant.factor``) is intentionally NOT called. Per ``CLAUDE.md``:
因子库统一用 pandas.
"""

from __future__ import annotations

from research.factor_lib.base import (
    CORE_COLUMNS_FACTOR,
    MissingColumnError,
    compute_factor,
    validate_input_bars,
)
from research.factor_lib.liquidity import turnover_ratio
from research.factor_lib.mean_reversion import bollinger_z, rsi
from research.factor_lib.momentum import n_day_return
from research.factor_lib.trend import ma_deviation

__all__ = [
    "CORE_COLUMNS_FACTOR",
    "MissingColumnError",
    "bollinger_z",
    "compute_factor",
    "ma_deviation",
    "n_day_return",
    "rsi",
    "turnover_ratio",
    "validate_input_bars",
]
