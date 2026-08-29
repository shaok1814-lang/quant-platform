"""Post-ingest OHLCV data quality checks (W6.1.2).

Runs after ``akshare.fetch_daily_bars`` returns and before
``DuckStore.upsert_daily_bars`` writes. Catches the four
failure modes that have historically broken downstream backtests
or paper trades:

  * HARD-NaN: any NaN in the OHLCV core columns. A non-numeric
    price / volume is unrecoverable — the row MUST NOT be
    upserted (would corrupt indicator / signal calculations
    downstream).
  * HARD-OHLC: ``high < max(open, close)`` or ``low > min(open,
    close)``. Indicates a malformed response from the source.
  * HARD-DuplicateDate: same ``date`` appears more than once
    in one fetcher response. The DuckDB upsert uses
    (symbol, date) PK; a duplicate would silently overwrite
    one row with the other on the same call.
  * HARD-VolumeNonPositive: ``volume <= 0`` outside of
    legitimate suspensions (which akshare emits with NaN prices
    anyway, so we reject negative volume conservatively).
  * HARD-OutOfRangeDate: any row date > today (akshare can
    emit "future" rows near midnight with timezone drift; AKQuant
    chokes on out-of-order bars).
  * SOFT-OutlierReturn: ``|daily_return| > 0.20``. A-share main
    board limit is ±10%, ChiNext ±20% (since 2020-08); a 20%+ move
    indicates either a special event (ST suspension, ex-rights
    mishandling) OR a fetcher glitch. SOFT so legitimate 20%
    ChiNext moves don't get rejected.

Design choice: collect ALL issues, don't raise on the first
one — so the caller (and the 钉钉 alert) gets the full picture,
not just the first defect. Caller pattern::

    report = check_quality(df, symbol="000001")
    if report.has_hard_issues:
        notify.ding("OHLCV quality failure", report.to_markdown())
        return  # do NOT upsert

If no hard issues, upsert is safe; SOFT issues are still
forwarded to the alert channel so the operator can review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_cls
from enum import StrEnum
from typing import Final

import numpy as np
import pandas as pd
from loguru import logger

__all__ = [
    "MAX_DAILY_RETURN_PCT",
    "REQUIRED_OHLCV_COLUMNS",
    "Issue",
    "IssueSeverity",
    "QualityReport",
    "check_quality",
]


class IssueSeverity(StrEnum):
    """Severity tag on a single :class:`Issue`.

    ``str`` mixin so loguru / JSON serialization treat it like a
    plain string without a custom encoder.

    Values:
        HARD: Reject (skip upsert). The row / df cannot be trusted
            downstream (would corrupt indicators / signals).
        SOFT: Accept but flag. The row is plausibly valid (e.g.
            a legitimate ChiNext 20% move) but warrants human
            review via 钉钉 alert.
    """

    HARD = "HARD"
    SOFT = "SOFT"


REQUIRED_OHLCV_COLUMNS: Final[tuple[str, ...]] = (
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
)

# 20% daily move is the ChiNext upper limit (since 2020-08-24).
# Main board limit is 10%. Using 20% as the SOFT threshold means
# legitimate ChiNext / Star Market moves don't get rejected; the
# price we pay is that a 25% ST-stock move (A-share ST limit is 5%
# BUT pre/post limit days can spike) goes un-flagged. Acceptable
# for W6.1 — escalate to per-stock limits in W6.2 if alerting noise
# gets worse.
MAX_DAILY_RETURN_PCT: Final[float] = 0.20


@dataclass(frozen=True)
class Issue:
    """One quality finding.

    Attributes:
        severity: :class:`IssueSeverity`.
        kind: Short machine-readable tag (``"NaN_OHLCV"``,
            ``"OHLC_INCONSISTENT"``, etc.). Used by 钉钉 alert
            templates to bucket similar issues.
        message: Human-readable description (English; the
            dashboard / alert pipeline can translate as needed).
        date: The offending date, if the issue is row-scoped.
            ``None`` for df-wide issues (e.g. duplicate dates).
    """

    severity: IssueSeverity
    kind: str
    message: str
    date: pd.Timestamp | None = None


@dataclass(frozen=True)
class QualityReport:
    """Aggregated quality verdict for a single fetcher response.

    Attributes:
        symbol: The 6-digit symbol the df was for (used in alerts).
        n_rows: Number of bars in the input df.
        issues: List of :class:`Issue` found. May be empty.
    """

    symbol: str
    n_rows: int
    issues: list[Issue] = field(default_factory=list)

    @property
    def has_hard_issues(self) -> bool:
        """``True`` if any issue is HARD.

        Caller must skip the upsert if so.
        """
        return any(i.severity == IssueSeverity.HARD for i in self.issues)

    @property
    def has_soft_issues(self) -> bool:
        """``True`` if any issue is SOFT (caller can still upsert)."""
        return any(i.severity == IssueSeverity.SOFT for i in self.issues)

    def to_markdown(self) -> str:
        """Render as a DingTalk-friendly markdown block.

        Appended verbatim to ``ops.notify.ding`` messages. Format:

            symbol=000001 rows=479 HARD=0 SOFT=1
            - SOFT OUTLIER_RETURN 2026-08-25: |return|=0.21

        Caller wraps the title and prepends 钉Talk at-rules.
        """
        n_hard = sum(1 for i in self.issues if i.severity == IssueSeverity.HARD)
        n_soft = sum(1 for i in self.issues if i.severity == IssueSeverity.SOFT)
        lines = [
            f"symbol={self.symbol} rows={self.n_rows} HARD={n_hard} SOFT={n_soft}",
        ]
        for i in self.issues:
            date_str = i.date.strftime("%Y-%m-%d") if i.date is not None else "df-wide"
            lines.append(f"- {i.severity.value} {i.kind} {date_str}: {i.message}")
        return "\n".join(lines)


def _check_columns(df: pd.DataFrame) -> list[Issue]:
    """HARD: required OHLCV columns present and non-empty."""
    issues: list[Issue] = []
    missing = [c for c in REQUIRED_OHLCV_COLUMNS if c not in df.columns]
    if missing:
        issues.append(
            Issue(
                severity=IssueSeverity.HARD,
                kind="MISSING_COLUMNS",
                message=f"missing required columns: {missing}",
            )
        )
        return issues  # can't continue without columns
    # NaN in OHLCV is a HARD reject — would corrupt downstream.
    for col in REQUIRED_OHLCV_COLUMNS[1:]:  # skip 'date'
        nan_mask = df[col].isna()
        if nan_mask.any():
            for ts in df.loc[nan_mask, "date"]:
                issues.append(
                    Issue(
                        severity=IssueSeverity.HARD,
                        kind=f"NAN_{col.upper()}",
                        message=f"{col} is NaN",
                        date=pd.Timestamp(ts),
                    )
                )
    return issues


def _check_ohlc_sanity(df: pd.DataFrame) -> list[Issue]:
    """HARD: high >= max(open, close) AND low <= min(open, close).

    Run as numeric comparison; NaN columns would have been
    caught upstream, but be defensive.
    """
    if not all(c in df.columns for c in ("open", "high", "low", "close", "date")):
        return []
    issues: list[Issue] = []
    # ``np.where`` returns the boolean mask; ``.any(axis=...)`` over
    # a row-wise boolean frame would be cleaner but ``np.where`` keeps
    # the dtype explicit.
    bad_high = df["high"] < np.maximum(df["open"], df["close"])
    bad_low = df["low"] > np.minimum(df["open"], df["close"])
    for mask, kind in (
        (bad_high, "OHLC_HIGH_BELOW_OPENCLOSE"),
        (bad_low, "OHLC_LOW_ABOVE_OPENCLOSE"),
    ):
        for ts in df.loc[mask, "date"]:
            issues.append(
                Issue(
                    severity=IssueSeverity.HARD,
                    kind=kind,
                    message="OHLC inconsistency",
                    date=pd.Timestamp(ts),
                )
            )
    return issues


def _check_volume_non_positive(df: pd.DataFrame) -> list[Issue]:
    """HARD: volume must be > 0 (akshare uses 0 for suspension;
    legitimate suspension then comes with NaN prices which the
    NaN check catches, so volume==0 surviving here is a glitch)."""
    if "volume" not in df.columns or "date" not in df.columns:
        return []
    issues: list[Issue] = []
    mask = df["volume"] <= 0
    for ts in df.loc[mask, "date"]:
        issues.append(
            Issue(
                severity=IssueSeverity.HARD,
                kind="VOLUME_NON_POSITIVE",
                message=f"volume={df.loc[df['date'] == ts, 'volume'].iloc[0]}",
                date=pd.Timestamp(ts),
            )
        )
    return issues


def _check_duplicate_dates(df: pd.DataFrame) -> list[Issue]:
    """HARD: same date appearing more than once (would clobber on
    upsert against the (symbol, date) PK)."""
    if "date" not in df.columns:
        return []
    issues: list[Issue] = []
    dup_mask = df["date"].duplicated(keep=False)
    if dup_mask.any():
        unique_dup_dates = df.loc[dup_mask, "date"].unique()
        for ts in unique_dup_dates:
            issues.append(
                Issue(
                    severity=IssueSeverity.HARD,
                    kind="DUPLICATE_DATE",
                    message="date appears more than once in fetch",
                    date=pd.Timestamp(ts),
                )
            )
    return issues


def _check_out_of_range_dates(df: pd.DataFrame) -> list[Issue]:
    """HARD: any date > today (timezone-cliff near midnight CST)."""
    if "date" not in df.columns:
        return []
    issues: list[Issue] = []
    today = pd.Timestamp(date_cls.today())
    mask = pd.to_datetime(df["date"]) > today
    for ts in df.loc[mask, "date"]:
        issues.append(
            Issue(
                severity=IssueSeverity.HARD,
                kind="FUTURE_DATE",
                message=f"date {pd.Timestamp(ts).date()} is after today",
                date=pd.Timestamp(ts),
            )
        )
    return issues


def _check_outlier_returns(df: pd.DataFrame) -> list[Issue]:
    """SOFT: |daily_return| > MAX_DAILY_RETURN_PCT.

    Need at least 2 rows to compute returns; if only 1, skip
    (single-day fetches legitimately have no return).
    """
    if not all(c in df.columns for c in ("close", "date")) or len(df) < 2:
        return []
    issues: list[Issue] = []
    # Sort by date to ensure prev/next alignment is correct
    # regardless of fetch-order.
    sorted_df = df.sort_values("date")
    close = sorted_df["close"].astype(float)
    prev = close.shift(1)
    ret = (close - prev) / prev
    abs_ret = ret.abs()
    mask = abs_ret > MAX_DAILY_RETURN_PCT
    for ts, r in zip(sorted_df.loc[mask, "date"], abs_ret[mask], strict=False):
        issues.append(
            Issue(
                severity=IssueSeverity.SOFT,
                kind="OUTLIER_RETURN",
                message=f"|return|={float(r):.2%} > {MAX_DAILY_RETURN_PCT:.0%}",
                date=pd.Timestamp(ts),
            )
        )
    return issues


def check_quality(df: pd.DataFrame, *, symbol: str) -> QualityReport:
    """Run all data-quality checks on a fetcher response.

    Args:
        df: Output of ``akshare.fetch_daily_bars``. Must contain
            the columns in :data:`REQUIRED_OHLCV_COLUMNS`.
        symbol: 6-digit symbol, used for the report header.

    Returns:
        :class:`QualityReport` aggregating every issue found.
        Always returns a report — never raises — so the caller
        can decide what to do.

    Side effect:
        Logs at INFO if clean, WARNING if SOFT issues, ERROR if
        HARD issues (so loguru-based alerts catch HARD without
        needing 钉钉 wiring).
    """
    if df.empty:
        logger.info("quality check symbol={s}: empty df, no rows", s=symbol)
        return QualityReport(symbol=symbol, n_rows=0, issues=[])

    issues: list[Issue] = []
    issues.extend(_check_columns(df))
    # Skip row-scoped checks if columns are missing (a MISSING_COLUMNS
    # HARD issue was already raised).
    if not any(i.kind == "MISSING_COLUMNS" for i in issues):
        issues.extend(_check_ohlc_sanity(df))
        issues.extend(_check_volume_non_positive(df))
        issues.extend(_check_duplicate_dates(df))
        issues.extend(_check_out_of_range_dates(df))
        issues.extend(_check_outlier_returns(df))

    report = QualityReport(symbol=symbol, n_rows=len(df), issues=issues)
    if report.has_hard_issues:
        logger.error(
            "quality check symbol={s}: HARD issues found ({n})",
            s=symbol,
            n=sum(1 for i in issues if i.severity == IssueSeverity.HARD),
        )
    elif report.has_soft_issues:
        logger.warning(
            "quality check symbol={s}: SOFT issues found ({n})",
            s=symbol,
            n=sum(1 for i in issues if i.severity == IssueSeverity.SOFT),
        )
    else:
        logger.info("quality check symbol={s}: clean ({n} rows)", s=symbol, n=len(df))
    return report
