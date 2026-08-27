"""Cross-source diff and validator for daily-bar DataFrames.

Aligns two fetcher outputs on ``date`` and reports per-date close
gap (in absolute price units and basis points) plus a threshold
pass / fail summary. Used by W2.2 to detect drift between akshare
and baostock daily closes.

Tolerance: 50 bps (0.5%) by default — covers akshare's documented
intra-day stitching noise and the small qfq ratio rounding gap
between sources. Tighten to 5 bps once we have multiple months of
in-sample data to set a tighter empirical baseline.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# Default tolerance in basis points. 50 bps == 0.5% absolute close gap.
DEFAULT_THRESHOLD_BPS: float = 50.0


@dataclass
class ValidationReport:
    """Result of a cross-source diff.

    Attributes
    ----------
    symbol, start_date, end_date : str
        Echoed from the input DataFrames' ``df.attrs['symbol']`` /
        ``date`` range, used for logging and report filenames.
    n_overlap : int
        Number of dates where both sources have a row.
    n_diff_exceed_threshold : int
        Number of overlapping dates whose ``pct_diff_bps`` exceeded
        ``threshold_bps``.
    max_abs_diff : float
        Largest absolute close gap across all overlapping dates.
    max_pct_diff_bps : float
        Largest pct close gap in basis points.
    mean_pct_diff_bps : float
        Mean pct close gap in basis points across the overlap.
    threshold_bps : float
        The threshold passed in (echoed for report rendering).
    diffs : pd.DataFrame
        Per-date diff rows with columns ``date``, ``close_a``,
        ``close_b``, ``abs_diff``, ``pct_diff_bps``.
    fetcher_a, fetcher_b : str
        Echoed from the input DataFrames' ``df.attrs['fetcher']`` so
        the report is self-describing.
    """

    symbol: str
    start_date: str
    end_date: str
    fetcher_a: str
    fetcher_b: str
    n_overlap: int
    n_diff_exceed_threshold: int
    max_abs_diff: float
    max_pct_diff_bps: float
    mean_pct_diff_bps: float
    threshold_bps: float
    diffs: pd.DataFrame

    @property
    def passed(self) -> bool:
        """True iff zero rows exceeded the threshold."""
        return self.n_diff_exceed_threshold == 0


def diff_sources(
    df_a: pd.DataFrame,
    df_b: pd.DataFrame,
    *,
    label_a: str | None = None,
    label_b: str | None = None,
) -> pd.DataFrame:
    """Compute per-date close diff between two fetcher outputs.

    Joins on ``date`` (inner join — only overlapping dates), sorts by
    date ascending, and emits columns ``date``, ``close_a``,
    ``close_b``, ``abs_diff``, ``pct_diff_bps``.

    ``pct_diff_bps`` is ``abs_diff / mean(close_a, close_b) * 10000``,
    which is symmetric and avoids sign issues when one side is
    missing the other.
    """
    if "date" not in df_a.columns or "date" not in df_b.columns:
        raise ValueError("both DataFrames must have a 'date' column")
    if "close" not in df_a.columns or "close" not in df_b.columns:
        raise ValueError("both DataFrames must have a 'close' column")

    a_name = label_a or df_a.attrs.get("fetcher", "a")
    b_name = label_b or df_b.attrs.get("fetcher", "b")

    a = df_a.loc[:, ["date", "close"]].rename(columns={"close": "close_a"})
    b = df_b.loc[:, ["date", "close"]].rename(columns={"close": "close_b"})
    merged = a.merge(b, on="date", how="inner").sort_values("date").reset_index(drop=True)

    if merged.empty:
        out = merged.assign(
            abs_diff=pd.Series(dtype="float64"),
            pct_diff_bps=pd.Series(dtype="float64"),
        )
        out.attrs["fetcher_a"] = a_name
        out.attrs["fetcher_b"] = b_name
        return out

    abs_diff = (merged["close_a"] - merged["close_b"]).abs()
    mean_close = (merged["close_a"] + merged["close_b"]) / 2.0
    pct_bps = abs_diff / mean_close * 10_000.0
    out = merged.assign(
        abs_diff=abs_diff,
        pct_diff_bps=pct_bps,
    )
    out.attrs["fetcher_a"] = a_name
    out.attrs["fetcher_b"] = b_name
    return out


def validate(
    df_a: pd.DataFrame,
    df_b: pd.DataFrame,
    *,
    threshold_bps: float = DEFAULT_THRESHOLD_BPS,
    label_a: str | None = None,
    label_b: str | None = None,
) -> ValidationReport:
    """Compare two fetcher outputs and produce a :class:`ValidationReport`.

    Parameters
    ----------
    df_a, df_b : pd.DataFrame
        Output of two fetcher calls (or DuckStore reads). Must share
        ``date`` and ``close`` columns.
    threshold_bps : float
        Per-row tolerance. Default 50 bps (0.5%). Rows above this
        flag as ``n_diff_exceed_threshold``.
    """
    diffs = diff_sources(df_a, df_b, label_a=label_a, label_b=label_b)

    if diffs.empty:
        return ValidationReport(
            symbol=str(df_a.attrs.get("symbol", "")),
            start_date="",
            end_date="",
            fetcher_a=diffs.attrs.get("fetcher_a", label_a or ""),
            fetcher_b=diffs.attrs.get("fetcher_b", label_b or ""),
            n_overlap=0,
            n_diff_exceed_threshold=0,
            max_abs_diff=0.0,
            max_pct_diff_bps=0.0,
            mean_pct_diff_bps=0.0,
            threshold_bps=threshold_bps,
            diffs=diffs,
        )

    exceed = diffs["pct_diff_bps"] > threshold_bps
    symbol = str(df_a.attrs.get("symbol", ""))
    return ValidationReport(
        symbol=symbol,
        start_date=str(diffs["date"].min().date()),
        end_date=str(diffs["date"].max().date()),
        fetcher_a=diffs.attrs.get("fetcher_a", label_a or ""),
        fetcher_b=diffs.attrs.get("fetcher_b", label_b or ""),
        n_overlap=len(diffs),
        n_diff_exceed_threshold=int(exceed.sum()),
        max_abs_diff=float(diffs["abs_diff"].max()),
        max_pct_diff_bps=float(diffs["pct_diff_bps"].max()),
        mean_pct_diff_bps=float(diffs["pct_diff_bps"].mean()),
        threshold_bps=threshold_bps,
        diffs=diffs,
    )
