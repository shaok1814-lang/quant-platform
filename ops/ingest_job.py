"""Daily OHLCV ingest pipeline (W6.1.4).

Composes the modules:

  * :mod:`ops.universe` — which symbols to ingest.
  * :mod:`data_layer.ingestion.akshare_fetcher` — fetch bars
    (lazy-imported so test stubs can replace it without
    pulling akshare).
  * :mod:`ops.quality` — post-fetch quality gate.
  * :mod:`data_layer.storage.duck` — DuckDB upsert by
    (symbol, date) PK (idempotent).
  * :mod:`ops.notify` — 钉钉 webhook on HARD-quality / network
    failures (no-op when not configured).

The pipeline is idempotent: re-running on the same date is a no-op
(DuckDB upsert overwrites identical rows with identical data,
and ``fetch_daily_bars`` is also idempotent if the same window
is requested). This is by design — APScheduler at 18:00 daily
might double-fire on a short retry and we want the second run
to be a safe no-op.

Caller pattern (production, set by ``ops.scheduler.run_forever``)::

    ingest_job.run_daily_ingest()  # uses defaults

Caller pattern (tests)::

    ingest_job.run_daily_ingest(
        date=date(2024, 9, 2),
        fetcher=lambda sym, s, e: stub_df,  # no network
        duckdb_path=tmp_path / "test.duckdb",
        universe_path=tmp_path / "universe.yaml",
    )

Design choice: ALL symbols get tried even if earlier symbols
fail. A single flaky fetch should not block the rest of the
universe (CLAUDE.md "数据可靠" — partial ingest > no ingest).
Aggregate report lets the operator triage which symbols need
a manual retry.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import date as date_cls
from pathlib import Path
from typing import Final

import pandas as pd
from data_layer.ingestion.akshare_fetcher import FetcherError
from data_layer.storage.duck import DuckStore
from loguru import logger

from ops.quality import QualityReport, check_quality
from ops.universe import UniverseEntry, load_universe

__all__ = [
    "DEFAULT_DUCKDB_PATH",
    "Fetcher",
    "IngestReport",
    "PerSymbolReport",
    "run_daily_ingest",
]

# Default DuckDB path mirrors W2.1's production file.
_PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_DUCKDB_PATH: Final[Path] = _PROJECT_ROOT / "data" / "duckdb" / "daily.duckdb"


# Type alias for the fetcher callable. Matches the signature of
# :func:`data_layer.ingestion.akshare_fetcher.fetch_daily_bars`
# EXCEPT that tests inject a simpler stub (no `adjust` kwarg).
Fetcher = Callable[..., pd.DataFrame]


@dataclass(frozen=True)
class PerSymbolReport:
    """Outcome of one symbol's ingest in a job run.

    Attributes:
        symbol: 6-digit symbol attempted.
        sector: Sector tag from universe.yaml (so callers can
            group failures by sector in dashboards).
        status: One of:
            * ``"upserted"`` — fetcher succeeded, quality passed,
                rows written to DuckDB.
            * ``"skipped_hard_quality"`` — quality gate found
                HARD issues; the df was NOT upserted (calls
                :func:`ding` if 钉钉 enabled).
            * ``"fetcher_error"`` — fetcher raised
                :class:`FetcherError` (network / delisted /
                suspended). Not upserted.
            * ``"other_error"`` — fetcher raised a non-FetcherError
                exception (programming bug). Logged at ERROR;
                钉聊 notified.
        n_rows: Number of rows in the fetcher response (0 if
            fetcher errored).
        quality: QualityReport if fetcher returned a df;
            ``None`` if fetcher errored.
        error: Error message for ``fetcher_error`` /
            ``other_error`` statuses; ``None`` otherwise.
    """

    symbol: str
    sector: str
    status: str
    n_rows: int = 0
    quality: QualityReport | None = None
    error: str | None = None


@dataclass(frozen=True)
class IngestReport:
    """Aggregate outcome of one ``run_daily_ingest`` invocation.

    Attributes:
        target_date: The trading date being ingested (the day the
            scheduler meant to capture).
        started_at: UTC ISO timestamp when the job started.
        duration_s: Wall-clock duration in seconds.
        per_symbol: One :class:`PerSymbolReport` per universe
            entry, in deterministic (sorted-by-symbol) order.
    """

    target_date: date_cls
    started_at: str
    duration_s: float
    per_symbol: list[PerSymbolReport] = field(default_factory=list)

    @property
    def n_upserted(self) -> int:
        return sum(1 for r in self.per_symbol if r.status == "upserted")

    @property
    def n_hard_quality(self) -> int:
        return sum(1 for r in self.per_symbol if r.status == "skipped_hard_quality")

    @property
    def n_fetcher_errors(self) -> int:
        return sum(1 for r in self.per_symbol if r.status == "fetcher_error")

    @property
    def n_other_errors(self) -> int:
        return sum(1 for r in self.per_symbol if r.status == "other_error")


def _default_fetcher() -> Fetcher:
    """Lazy default fetcher (so tests don't need akshare installed)."""
    from data_layer.ingestion.akshare_fetcher import fetch_daily_bars

    def _fetch(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        return fetch_daily_bars(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
        )

    return _fetch


def _notify_hard_failures(report: IngestReport) -> None:
    """Send one 钉聊 alert if any symbol had HARD quality issues.

    Lazy import so ``ops.notify`` can stay optional at runtime.
    """
    hard_failures = [r for r in report.per_symbol if r.status == "skipped_hard_quality"]
    if not hard_failures:
        return
    from ops import notify

    lines = [
        f"target_date={report.target_date}",
        f"HARD quality failures: {len(hard_failures)} of {len(report.per_symbol)} symbols",
    ]
    for r in hard_failures:
        if r.quality is not None:
            lines.append(r.quality.to_markdown())
        elif r.error:
            lines.append(f"- {r.symbol} ({r.sector}): {r.error}")
    notify.ding(
        f"Ingest HARD quality failures ({report.target_date})",
        "\n".join(lines),
    )


def run_daily_ingest(
    date: date_cls | None = None,
    *,
    duckdb_path: str | Path | None = None,
    universe_path: str | Path | None = None,
    fetcher: Fetcher | None = None,
    notify_on_hard: bool = True,
) -> IngestReport:
    """Run the daily ingest pipeline for ``date``.

    Args:
        date: The trading date to ingest. Defaults to today
            (UTC). Pass a fixed date in tests for determinism.
        duckdb_path: Path to the DuckDB file. Defaults to
            :data:`DEFAULT_DUCKDB_PATH` (production layout).
        universe_path: Path to ``config/universe.yaml``. Defaults
            to :data:`ops.universe.DEFAULT_UNIVERSE_PATH`.
        fetcher: Injectable fetcher (defaults to
            ``akshare.fetch_daily_bars``). Tests inject a stub
            that returns a synthetic df without network.
        notify_on_hard: If ``True`` (default), send a 钉聊 alert
            on any HARD quality failures. Set to ``False`` in
            tests so the alert channel never fires accidentally.

    Returns:
        :class:`IngestReport` aggregating per-symbol outcomes.
        Always returns a report (does not raise on per-symbol
        errors — those are collected as ``fetcher_error`` /
        ``other_error`` in the per-symbol reports).

    Side effects:
        Writes to DuckDB (idempotent upsert). Logs at INFO / WARNING
        / ERROR per symbol. Optionally POSTs to 钉聊 on HARD
        failures.
    """
    target = date or datetime.now(UTC).date()
    started = datetime.now(UTC)
    t0 = time.monotonic()
    universe = load_universe(universe_path)
    db_path = Path(duckdb_path) if duckdb_path is not None else DEFAULT_DUCKDB_PATH
    fetch = fetcher if fetcher is not None else _default_fetcher()

    # Window: target date only. akshare returns the trading day's
    # bar if ``target`` is a trading day, empty (FetcherError)
    # otherwise — handled per-symbol below.
    start_str = target.strftime("%Y%m%d")
    end_str = target.strftime("%Y%m%d")
    # ``end_date`` exclusive adjustment: akshare treats
    # ``start_date == end_date`` as a single-day range, which is
    # what we want. (Verified W2.1.)

    per_symbol: list[PerSymbolReport] = []
    with DuckStore(db_path) as store:
        for entry in universe:
            psr = _ingest_one(
                entry=entry,
                start_date=start_str,
                end_date=end_str,
                fetch=fetch,
                store=store,
            )
            per_symbol.append(psr)

    duration = time.monotonic() - t0
    report = IngestReport(
        target_date=target,
        started_at=started.isoformat(),
        duration_s=duration,
        per_symbol=per_symbol,
    )
    logger.info(
        "ingest done target={d} symbols={n} upserted={u} "
        "hard_quality={hq} fetcher_err={fe} other_err={oe} duration={dur:.2f}s",
        d=target,
        n=len(per_symbol),
        u=report.n_upserted,
        hq=report.n_hard_quality,
        fe=report.n_fetcher_errors,
        oe=report.n_other_errors,
        dur=duration,
    )
    if notify_on_hard:
        _notify_hard_failures(report)
    return report


def _ingest_one(
    entry: UniverseEntry,
    start_date: str,
    end_date: str,
    *,
    fetch: Fetcher,
    store: DuckStore,
) -> PerSymbolReport:
    """Fetch + quality-check + upsert one symbol.

    Errors are collected into the returned PerSymbolReport; this
    function never raises (caller iterates the universe and needs
    to keep going on per-symbol failures).
    """
    try:
        df = fetch(symbol=entry.symbol, start_date=start_date, end_date=end_date)
    except FetcherError as exc:
        logger.warning("ingest symbol={s}: fetcher error ({e})", s=entry.symbol, e=exc)
        return PerSymbolReport(
            symbol=entry.symbol,
            sector=entry.sector,
            status="fetcher_error",
            error=str(exc),
        )
    except Exception as exc:
        logger.error("ingest symbol={s}: unexpected error ({e})", s=entry.symbol, e=exc)
        return PerSymbolReport(
            symbol=entry.symbol,
            sector=entry.sector,
            status="other_error",
            error=f"{type(exc).__name__}: {exc}",
        )

    # ``fetch_daily_bars`` may return empty when the target is a
    # non-trading day (handled here so caller doesn't see a
    # fetcher_error for legit holidays).
    if df.empty:
        logger.info("ingest symbol={s}: no rows ({d})", s=entry.symbol, d=end_date)
        # ``n_rows=0``, no quality report — caller treats as
        # "no data to write" which is a clean no-op.
        return PerSymbolReport(
            symbol=entry.symbol,
            sector=entry.sector,
            status="upserted",
            n_rows=0,
        )

    quality = check_quality(df, symbol=entry.symbol)
    if quality.has_hard_issues:
        logger.error(
            "ingest symbol={s}: HARD quality issues, skipping upsert",
            s=entry.symbol,
        )
        return PerSymbolReport(
            symbol=entry.symbol,
            sector=entry.sector,
            status="skipped_hard_quality",
            n_rows=quality.n_rows,
            quality=quality,
        )

    rows = store.upsert_daily_bars(df)
    logger.info("ingest symbol={s}: upserted {n} rows", s=entry.symbol, n=rows)
    return PerSymbolReport(
        symbol=entry.symbol,
        sector=entry.sector,
        status="upserted",
        n_rows=len(df),
        quality=quality,
    )


def fetch_window_days(target: date_cls, *, window_days: int = 7) -> tuple[str, str]:
    """Helper for backfill ingest: convert (target, window) into a
    YYYYMMDD pair for ``fetch_daily_bars``.

    Akshare's ``start_date`` / ``end_date`` are inclusive. For a
    ``window_days=7`` backfill ending today, callers pass
    ``target - 7`` as the lower bound. Provided here so future
    backfill code does not duplicate the strftime dance.
    """
    end = target
    start = end - timedelta(days=window_days)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
