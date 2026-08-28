"""FactorPipeline: compose N factors + post-processing into one ``compute(df)`` call.

The pipeline is the primary research-layer API: a single dataclass
holds the factor list, the winsorize method, the standardize toggle,
the neutralizer hook, and the output format. ``compute(df)`` runs
all factors, applies cross-section post-processing per date,
neutralizes (if a non-pass-through neutralizer is configured), and
returns either wide or long format.

Cross-section semantics:
    * If the input ``df`` carries ``date`` (in a column or in a
      ``DatetimeIndex`` / ``MultiIndex`` whose first level is named
      ``"date"``), the pipeline groups rows by date and applies
      winsorize / standardize per group.
    * Otherwise (single-symbol with no date reference) post-processing
      is applied globally.

Neutralizer semantics:
    * Only meaningful for multi-symbol input (otherwise per-date is
      per-row).
    * The neutralizer's ``__call__`` receives a wide DataFrame with
      one row per ``(date, symbol)`` plus the ``factor_col`` it
      should adjust and the ``group_col`` to group by. W3's
      ``PassThroughNeutralizer`` returns the input unchanged so
      pipelines can ship without industry mapping data (W5).

Output format:
    * ``"long"`` (default): ``pd.DataFrame`` with columns
      ``[date, symbol, factor_name, factor_value]``. PK-friendly for
      a future DuckDB ``factor_values`` table; cross-symbol ranking
      is a one-liner with pivot; W5 panel datasets do not bloat
      with N factor columns.
    * ``"wide"``: ``pd.DataFrame`` with one column per factor, the
      ``(date, symbol)`` index preserved. Convenient for ad-hoc
      Jupyter exploration.

Implementation note (date / symbol handling):
    The pipeline does NOT assume a fixed input shape — single-symbol
    bars frames have ``date`` as a column with a RangeIndex, while
    multi-symbol bars frames commonly arrive as ``MultiIndex(date,
    symbol)``. We extract ``date`` / ``symbol`` via
    :func:`_extract_date_series` and :func:`_extract_symbol_series`
    (which probe columns first, then index) and stash them in
    ``wide`` under private ``__date__`` / ``__symbol__`` column names
    so the post-processing, neutralizer, and melt phases all see a
    consistent view.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from research.factor_lib._types import BarsDF
from research.factor_lib.base import validate_input_bars
from research.factor_lib.post import (
    Neutralizer,
    PassThroughNeutralizer,
    WinsorMethod,
    standardize,
    winsorize,
)

__all__ = ["LONG_FORMAT_COLUMNS", "WIDE_FORMAT_INDEX_NAMES", "FactorPipeline"]

# Canonical column order for long-format output. Defined as a tuple
# so ``pd.DataFrame(columns=...)`` and asserts can both reuse it.
LONG_FORMAT_COLUMNS: tuple[str, ...] = ("date", "symbol", "factor_name", "factor_value")

# Index name expected on wide-format output. The pipeline always
# preserves the input index; consumers that need both ``date`` and
# ``symbol`` to be named can use these constants.
WIDE_FORMAT_INDEX_NAMES: tuple[str, ...] = ("date", "symbol")

OutputFormat = Literal["wide", "long"]

# Private column names used internally to shuttle date / symbol
# through the wide frame. Underscored to discourage downstream
# consumers from depending on them.
_DATE_COL = "__date__"
_SYMBOL_COL = "__symbol__"


@dataclass(frozen=True)
class FactorPipeline:
    """Composable factor pipeline.

    Attributes:
        factors: Ordered ``(name, fn)`` pairs. ``fn`` must accept a
            ``BarsDF`` (i.e. the input frame as-is) and return a
            ``pd.Series`` aligned to ``df.index`` with
            ``series.name`` set to a stable factor name. The
            pipeline overrides ``series.name`` to the entry's
            ``name`` so factor ordering and naming stay explicit.
        winsorize_method: Cross-section winsorize strategy. Default
            ``"3sigma"``.
        standardize: If True, z-score standardize after winsorize.
        neutralizer: Cross-section neutralizer hook. Default
            ``PassThroughNeutralizer``. W5 will introduce
            ``IndustryNeutralizer``.
        output_format: ``"long"`` (default) or ``"wide"``.

    Example:
        >>> pipeline = FactorPipeline(
        ...     factors=(
        ...         ("ma_dev_20", lambda d: ma_deviation(d["close"], bar_window=20)),
        ...         ("nret_20", lambda d: n_day_return(d["close"], window=20)),
        ...     ),
        ... )
        >>> long_df = pipeline.compute(df)  # doctest: +SKIP
    """

    factors: tuple[tuple[str, Callable[[BarsDF], pd.Series]], ...]
    winsorize_method: WinsorMethod = "3sigma"
    standardize: bool = True
    neutralizer: Neutralizer | None = None
    output_format: OutputFormat = "long"

    def __post_init__(self) -> None:
        # ``Neutralizer`` is a Protocol — a default value would force
        # callers to import the class even when they want the default.
        # Resolve None here so the rest of the code treats it as the
        # pass-through variant.
        if self.neutralizer is None:
            object.__setattr__(self, "neutralizer", PassThroughNeutralizer())

    def compute(self, df: BarsDF) -> pd.DataFrame:
        """Run all factors + post-processing on ``df``.

        Args:
            df: Bars DataFrame. Validated against ``CORE_COLUMNS_FACTOR``.

        Returns:
            ``pd.DataFrame``. See module docstring for the long /
            wide shape contract.
        """
        validate_input_bars(df)
        if df.empty:
            return self._empty_output(df)

        # 1. Compute each factor; rebuild a wide DataFrame aligned
        #    to df.index. Override ``series.name`` so the entry's
        #    ``name`` (not the factor fn's intrinsic name) controls
        #    the column label.
        wide = pd.concat(
            [_with_name(fn(df), name) for name, fn in self.factors],
            axis=1,
        )
        wide.index = df.index

        # 2. Snapshot date + symbol as private columns so the rest of
        #    the pipeline (groupby / neutralizer / melt) sees a
        #    uniform view regardless of whether the input had them in
        #    columns or in the index.
        date_keys = _extract_date_series(df)
        symbol_keys = _extract_symbol_series(df)
        if date_keys is not None:
            wide[_DATE_COL] = date_keys.values
        if symbol_keys is not None:
            wide[_SYMBOL_COL] = symbol_keys.values

        factor_only_cols = [
            c for c in wide.columns if c not in (_DATE_COL, _SYMBOL_COL)
        ]

        # 3. Cross-section post-processing per date group.
        if date_keys is not None:
            for col in factor_only_cols:
                wide[col] = wide[col].groupby(wide[_DATE_COL]).transform(self._postprocess)
        else:
            for col in factor_only_cols:
                wide[col] = self._postprocess(wide[col])

        # 4. Neutralizer — only meaningful when there are multiple
        #    symbols per date (otherwise per-date is per-row).
        if (
            date_keys is not None
            and symbol_keys is not None
            and not isinstance(self.neutralizer, PassThroughNeutralizer)
        ):
            wide = self._apply_neutralizer(wide, factor_only_cols)

        # 5. Output format.
        if self.output_format == "wide":
            return wide[factor_only_cols]
        return self._melt_long(wide, factor_only_cols, date_keys, symbol_keys)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _postprocess(self, s: pd.Series) -> pd.Series:
        out = winsorize(s, method=self.winsorize_method)
        if self.standardize:
            out = standardize(out)
        return out

    def _apply_neutralizer(
        self,
        wide: pd.DataFrame,
        factor_only_cols: list[str],
    ) -> pd.DataFrame:
        """Apply the neutralizer column-by-column, grouping by date.

        The neutralizer's contract is ``__call__(df_wide, factor_col,
        group_col)``. We expose ``_DATE_COL`` as the ``group_col``
        argument so the neutralizer can ``groupby(group_col)``.
        """
        out = wide.copy()
        for col in factor_only_cols:
            out = self.neutralizer(out, col, _DATE_COL)
        return out

    def _melt_long(
        self,
        wide: pd.DataFrame,
        factor_only_cols: list[str],
        date_keys: pd.Series | None,
        symbol_keys: pd.Series | None,
    ) -> pd.DataFrame:
        """Melt wide ``[factor cols + __date__ + __symbol__]`` into long form."""
        long_df = wide[factor_only_cols].copy()
        if date_keys is not None:
            long_df["date"] = wide[_DATE_COL].values
        if symbol_keys is not None:
            long_df["symbol"] = wide[_SYMBOL_COL].values
        elif "symbol" not in long_df.columns:
            long_df["symbol"] = "_"
        return long_df.melt(
            id_vars=[c for c in ("date", "symbol") if c in long_df.columns],
            var_name="factor_name",
            value_name="factor_value",
        )

    def _empty_output(self, df: BarsDF) -> pd.DataFrame:
        if self.output_format == "wide":
            return pd.DataFrame(index=df.index)
        return pd.DataFrame(columns=list(LONG_FORMAT_COLUMNS))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _with_name(s: pd.Series, name: str) -> pd.Series:
    """Return a copy of ``s`` with its name replaced by ``name``."""
    s = s.copy()
    s.name = name
    return s


def _extract_date_series(df: BarsDF) -> pd.Series | None:
    """Return a per-row date Series, probing columns then index.

    Resolution order:
      1. ``"date"`` column on ``df``.
      2. ``DatetimeIndex`` (single-level index named ``"date"``).
      3. ``MultiIndex`` whose first level is named ``"date"``.
      4. ``None`` if no date reference is available.
    """
    if "date" in df.columns:
        return df["date"].reset_index(drop=True)
    if isinstance(df.index, pd.DatetimeIndex):
        return pd.Series(df.index, name="date").reset_index(drop=True)
    if isinstance(df.index, pd.MultiIndex) and df.index.names[0] == "date":
        return pd.Series(df.index.get_level_values(0), name="date").reset_index(drop=True)
    return None


def _extract_symbol_series(df: BarsDF) -> pd.Series | None:
    """Return a per-row symbol Series (or None for single-symbol input)."""
    if "symbol" in df.columns:
        return df["symbol"].reset_index(drop=True)
    if (
        isinstance(df.index, pd.MultiIndex)
        and len(df.index.names) > 1
        and df.index.names[1] == "symbol"
    ):
        return pd.Series(df.index.get_level_values(1), name="symbol").reset_index(drop=True)
    return None
