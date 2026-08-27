"""Cross-source validation.

``cross_source.validate(df_a, df_b)`` aligns two fetcher outputs on
``date`` and reports the per-date basis-point gap plus a
threshold-based pass / fail summary. Used by W2.2 to detect drift
between akshare and baostock daily closes.
"""

from __future__ import annotations

from data_layer.validation.cross_source import (
    ValidationReport,
    diff_sources,
    validate,
)

__all__ = ["ValidationReport", "diff_sources", "validate"]
