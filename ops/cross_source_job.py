"""Cross-source validation job (W6.3).

For every symbol in ``config/universe.yaml``, fetch daily bars from
**akshare direct** AND **baostock direct** (NOT the W6.1 fallback —
we want to see both sources independently), then call
:func:`data_layer.validation.cross_source.validate` to compute the
per-date ``close`` diff in basis points. Aggregate into a
:class:`CrossSourceReport`.

Why this exists separately from :mod:`ops.ingest_job`:

* The ingest pipeline (``run_daily_ingest``) is a **fast path** that
  uses ``fetch_daily_bars_with_fallback`` — 1 HTTP call per symbol.
  Cross-source validation needs **2** HTTP calls per symbol (akshare
  direct + baostock direct). On a flaky network (W2.1 known issue:
  Windows netsh proxy intermittently RST's akshare's eastmoney TLS
  handshake) the second call is unreliable.
* The ingest pipeline never **compares** sources — it just upserts
  whichever one returned data. We have no automated way to know
  whether ``000001``'s row written today matches what the other
  source would have said. After W1 + W6.1, production DuckDB shows
  ``000001.fetchers = "akquant, baostock"`` — i.e. some rows came
  from AKQuant's bundled source, others from baostock, with no
  diff audit between them.
* This module is the audit. It does NOT write to DuckDB (the diff
  result is purely observational). It runs on demand (manual or
  weekly cron, NOT the W6.1 18:00 hot path) and emits a single
  钉聊 SOFT alert when ``n_failed > 0``.

Design choices mirroring :mod:`ops.ingest_job` for consistency:

* Per-symbol isolation: any single fetcher failure doesn't block
  other symbols.
* Idempotent: re-running on the same date yields the same report
  (the fetchers are deterministic for a fixed window).
* Default ``threshold_bps=50.0`` matches
  :data:`data_layer.validation.cross_source.DEFAULT_THRESHOLD_BPS`.
* ``notify_on_fail=False`` in tests so 钉聊 never fires accidentally.

Caller pattern (production, manual)::

    from ops.cross_source_job import run_cross_source_check
    r = run_cross_source_check()  # today's date, 38 symbols
    print(f"passed={r.n_passed} failed={r.n_failed} skipped={r.n_skipped}")

Caller pattern (tests)::

    run_cross_source_check(
        date=date(2026, 8, 31),
        universe_path=tmp_path / "universe.yaml",
        akshare_fetcher=lambda sym, s, e: stub_df,
        baostock_fetcher=lambda sym, s, e: stub_df,
        notify_on_fail=False,
    )
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import date as date_cls
from pathlib import Path
from typing import Final, Literal

import pandas as pd
from data_layer.validation.cross_source import validate
from loguru import logger

from ops.universe import UniverseEntry, load_universe

__all__ = [
    "DEFAULT_THRESHOLD_BPS",
    "CrossSourceReport",
    "PerSymbolDiffReport",
    "Status",
    "run_cross_source_check",
]

# Default cross-source threshold in basis points. Mirrors W2.2 default
# (:data:`data_layer.validation.cross_source.DEFAULT_THRESHOLD_BPS`)
# — 50bps == 0.5% absolute close gap, covers akshare's documented
# intra-day stitching noise and the qfq ratio rounding gap between
# sources. Future tightening requires in-sample baseline data, out of
# scope for W6.3.
DEFAULT_THRESHOLD_BPS: Final[float] = 50.0

# Type alias for the fetcher callable. Tests inject stubs; production
# uses the default ``fetch_daily_bars`` from each fetcher module. The
# signatures differ in date format (akshare: ``YYYYMMDD``; baostock:
# ``YYYY-MM-DD``), so two distinct aliases exist.
AkshareFetcher = Callable[[str, str, str], pd.DataFrame]
BaostockFetcher = Callable[[str, str, str], pd.DataFrame]

# Status literal for :class:`PerSymbolDiffReport`.
Status = Literal[
    "passed",  # both fetchers OK, validate.passed == True
    "failed",  # both fetchers OK, validate.passed == False
    "skipped_akshare",  # akshare raised / empty; baostock OK
    "skipped_baostock",  # baostock raised / empty; akshare OK
    "skipped_both",  # both raised / empty (e.g. non-trading day)
]


@dataclass(frozen=True)
class PerSymbolDiffReport:
    """Outcome of one symbol's cross-source diff.

    Attributes:
        symbol: 6-digit symbol.
        sector: Sector tag from universe.yaml.
        status: One of the :data:`Status` literals.
        n_overlap: Number of dates where both fetchers returned a
            row. ``0`` if status is anything other than ``passed``
            or ``failed``.
        max_pct_diff_bps: Largest per-date bps diff in the overlap
            region. ``0.0`` if no overlap.
        mean_pct_diff_bps: Mean per-date bps diff in the overlap
            region. ``0.0`` if no overlap.
        fetcher_a, fetcher_b: Echoed from the fetchers (always
            ``"akshare"`` and ``"baostock"`` for the default config,
            but exposed for test stubs).
        error: Error message if either fetcher raised. ``None`` if
            both succeeded.
    """

    symbol: str
    sector: str
    status: Status
    n_overlap: int = 0
    max_pct_diff_bps: float = 0.0
    mean_pct_diff_bps: float = 0.0
    fetcher_a: str = "akshare"
    fetcher_b: str = "baostock"
    error: str | None = None


@dataclass(frozen=True)
class CrossSourceReport:
    """Aggregate outcome of one cross-source check invocation.

    Attributes:
        target_date: The trading date checked.
        started_at: UTC ISO timestamp when the job started.
        duration_s: Wall-clock duration in seconds.
        threshold_bps: The bps threshold used (echoed for reporting).
        per_symbol: One :class:`PerSymbolDiffReport` per universe
            entry, in deterministic (sorted-by-symbol) order.
    """

    target_date: date_cls
    started_at: str
    duration_s: float
    threshold_bps: float
    per_symbol: list[PerSymbolDiffReport] = field(default_factory=list)

    @property
    def n_passed(self) -> int:
        """Count of symbols where ``status == "passed"``."""
        return sum(1 for r in self.per_symbol if r.status == "passed")

    @property
    def n_failed(self) -> int:
        """Count of symbols where ``status == "failed"`` (>=1 row
        exceeded the bps threshold). Drives SOFT 钉聊 alerting."""
        return sum(1 for r in self.per_symbol if r.status == "failed")

    @property
    def n_skipped(self) -> int:
        """Count of symbols where at least one fetcher failed
        (sum of ``skipped_akshare`` + ``skipped_baostock`` +
        ``skipped_both``)."""
        return sum(
            1
            for r in self.per_symbol
            if r.status in ("skipped_akshare", "skipped_baostock", "skipped_both")
        )


# ---------------------------------------------------------------------------
# Default fetchers (lazy so tests don't need akshare / baostock installed)
# ---------------------------------------------------------------------------


def _default_akshare_fetcher() -> AkshareFetcher:
    """Lazy default akshare direct fetcher.

    Uses the **direct** ``fetch_daily_bars`` (NOT the W6.1
    ``fetch_daily_bars_with_fallback``). The whole point of this
    module is to compare the two sources independently — falling
    back would defeat the purpose.
    """
    from data_layer.ingestion.akshare_fetcher import fetch_daily_bars

    def _fetch(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        return fetch_daily_bars(symbol, start_date, end_date, adjust="qfq")

    return _fetch


def _default_baostock_fetcher() -> BaostockFetcher:
    """Lazy default baostock direct fetcher (mirrors akshare one)."""
    from data_layer.ingestion.baostock_fetcher import fetch_daily_bars

    def _fetch(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        return fetch_daily_bars(symbol, start_date, end_date, adjust="qfq")

    return _fetch


# ---------------------------------------------------------------------------
# Date format helpers (akshare=YYYYMMDD, baostock=YYYY-MM-DD)
# ---------------------------------------------------------------------------


def _to_baostock_date(s: str) -> str:
    """Convert ``YYYYMMDD`` → ``YYYY-MM-DD`` (no-op if already dashed)."""
    if "-" in s:
        return s
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"date must be YYYYMMDD or YYYY-MM-DD, got {s!r}")
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


# ---------------------------------------------------------------------------
# Per-symbol diff
# ---------------------------------------------------------------------------


def _diff_one_symbol(
    entry: UniverseEntry,
    start_date: str,
    end_date: str,
    *,
    akshare_fetch: AkshareFetcher,
    baostock_fetch: BaostockFetcher,
    threshold_bps: float,
) -> PerSymbolDiffReport:
    """Fetch both sources for one symbol, run ``validate``, return report.

    Never raises — all fetcher / validation errors are caught and
    surfaced as a :class:`PerSymbolDiffReport` with a non-success
    status. Caller iterates the universe and needs every symbol to
    produce exactly one report.
    """
    df_a: pd.DataFrame | None = None
    df_b: pd.DataFrame | None = None
    err_a: str | None = None
    err_b: str | None = None

    # akshare — direct call; any exception (FetcherError or upstream
    # network / value error) is collected into the report rather
    # than raised, so the per-symbol loop never aborts.
    try:
        df_a = akshare_fetch(entry.symbol, start_date, end_date)
        if df_a.empty:
            df_a = None
            err_a = "empty response"
    except Exception as exc:
        logger.warning(
            "cross_source symbol={s}: akshare fetch failed ({e})",
            s=entry.symbol,
            e=exc,
        )
        err_a = f"{type(exc).__name__}: {exc}"

    # baostock — needs ISO format (YYYY-MM-DD).
    try:
        bs_start = _to_baostock_date(start_date)
        bs_end = _to_baostock_date(end_date)
        df_b = baostock_fetch(entry.symbol, bs_start, bs_end)
        if df_b.empty:
            df_b = None
            err_b = "empty response"
    except Exception as exc:
        logger.warning(
            "cross_source symbol={s}: baostock fetch failed ({e})",
            s=entry.symbol,
            e=exc,
        )
        err_b = f"{type(exc).__name__}: {exc}"

    # Branch on which side(s) succeeded.
    if df_a is None and df_b is None:
        return PerSymbolDiffReport(
            symbol=entry.symbol,
            sector=entry.sector,
            status="skipped_both",
            error=err_a or err_b or "both fetchers failed",
        )
    if df_a is None:
        return PerSymbolDiffReport(
            symbol=entry.symbol,
            sector=entry.sector,
            status="skipped_akshare",
            error=err_a,
        )
    if df_b is None:
        return PerSymbolDiffReport(
            symbol=entry.symbol,
            sector=entry.sector,
            status="skipped_baostock",
            error=err_b,
        )

    # Both succeeded — diff via W2.2's validate.
    try:
        vreport = validate(
            df_a,
            df_b,
            threshold_bps=threshold_bps,
            label_a="akshare",
            label_b="baostock",
        )
    except ValueError as exc:
        # ``validate`` raises on missing ``date``/``close`` columns;
        # shouldn't happen with the default fetchers, but a stub
        # might omit them.
        logger.error(
            "cross_source symbol={s}: validate raised ({e})",
            s=entry.symbol,
            e=exc,
        )
        return PerSymbolDiffReport(
            symbol=entry.symbol,
            sector=entry.sector,
            status="skipped_both",
            error=f"validate ValueError: {exc}",
        )

    status: Status = "passed" if vreport.passed else "failed"
    return PerSymbolDiffReport(
        symbol=entry.symbol,
        sector=entry.sector,
        status=status,
        n_overlap=vreport.n_overlap,
        max_pct_diff_bps=vreport.max_pct_diff_bps,
        mean_pct_diff_bps=vreport.mean_pct_diff_bps,
        fetcher_a=vreport.fetcher_a or "akshare",
        fetcher_b=vreport.fetcher_b or "baostock",
        error=None,
    )


# ---------------------------------------------------------------------------
# 钉聊 SOFT notification
# ---------------------------------------------------------------------------


def _notify_cross_source_failures(report: CrossSourceReport) -> None:
    """Send one 钉聊 alert iff ``n_failed > 0``.

    Mirrors :func:`ops.ingest_job._notify_hard_failures` in shape:
    single ``ding()`` per run with a markdown body listing the
    failed symbols. Skipped symbols are also surfaced (so the
    operator can tell "all OK" from "couldn't validate").

    Lazy import so :mod:`ops.notify` stays optional at runtime.
    """
    if report.n_failed == 0:
        return
    from ops import notify

    lines: list[str] = [
        f"target_date={report.target_date}",
        f"threshold={report.threshold_bps:.1f}bps  "
        f"symbols={len(report.per_symbol)}  "
        f"passed={report.n_passed}  failed={report.n_failed}  "
        f"skipped={report.n_skipped}",
    ]

    failed_lines = [
        f"- {r.symbol} ({r.sector}): max={r.max_pct_diff_bps:.1f}bps "
        f"mean={r.mean_pct_diff_bps:.1f}bps overlap={r.n_overlap} "
        f"({r.fetcher_a} vs {r.fetcher_b})"
        for r in report.per_symbol
        if r.status == "failed"
    ]
    if failed_lines:
        lines.append("")
        lines.append("failed symbols:")
        lines.extend(failed_lines)

    skipped_lines = [
        f"- {r.symbol} ({r.sector}): {r.status} ({r.error or 'no error msg'})"
        for r in report.per_symbol
        if r.status in ("skipped_akshare", "skipped_baostock", "skipped_both")
    ]
    if skipped_lines:
        lines.append("")
        lines.append("skipped symbols (network):")
        lines.extend(skipped_lines)

    notify.ding(
        f"Cross-source diff report ({report.target_date})",
        "\n".join(lines),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_cross_source_check(
    date: date_cls | None = None,
    *,
    universe_path: str | Path | None = None,
    akshare_fetcher: AkshareFetcher | None = None,
    baostock_fetcher: BaostockFetcher | None = None,
    threshold_bps: float = DEFAULT_THRESHOLD_BPS,
    notify_on_fail: bool = True,
) -> CrossSourceReport:
    """Run the dual-fetch cross-source check for ``date``.

    Args:
        date: Trading date to check. Defaults to today (UTC).
            Pass a fixed date in tests for determinism.
        universe_path: Path to ``config/universe.yaml``. Defaults
            to :data:`ops.universe.DEFAULT_UNIVERSE_PATH`.
        akshare_fetcher: Injectable akshare fetcher (defaults to
            :func:`data_layer.ingestion.akshare_fetcher.fetch_daily_bars`).
            Tests inject a stub returning a synthetic df without
            network. The default uses **direct** akshare (NOT the
            W6.1 fallback) on purpose — we want to compare sources
            independently.
        baostock_fetcher: Injectable baostock fetcher. Same
            convention as ``akshare_fetcher``.
        threshold_bps: Per-date ``close`` diff tolerance in basis
            points. Default 50bps (matches
            :data:`data_layer.validation.cross_source.DEFAULT_THRESHOLD_BPS`).
        notify_on_fail: If ``True`` (default), send a 钉聊 SOFT
            alert when ``n_failed > 0``. Set to ``False`` in tests
            so the alert channel never fires accidentally.

    Returns:
        :class:`CrossSourceReport` aggregating per-symbol outcomes.
        Always returns a report (does not raise on per-symbol
        errors — those are collected as ``skipped_*`` statuses).

    Side effects:
        2 HTTP calls per universe symbol (akshare + baostock
        direct). Idempotent — re-running on the same window yields
        comparable diffs. Optionally POSTs to 钉聊 on
        ``n_failed > 0``.
    """
    target = date or datetime.now(UTC).date()
    started = datetime.now(UTC)
    t0 = time.monotonic()
    universe = load_universe(universe_path)
    ak_fetch = akshare_fetcher if akshare_fetcher is not None else _default_akshare_fetcher()
    bs_fetch = baostock_fetcher if baostock_fetcher is not None else _default_baostock_fetcher()

    # akshare takes YYYYMMDD; baostock conversion happens inside
    # ``_diff_one_symbol``. Window: target date only — we want a
    # single-day snapshot, same shape as ``run_daily_ingest``.
    start_str = target.strftime("%Y%m%d")
    end_str = target.strftime("%Y%m%d")

    per_symbol: list[PerSymbolDiffReport] = []
    for entry in universe:
        psr = _diff_one_symbol(
            entry=entry,
            start_date=start_str,
            end_date=end_str,
            akshare_fetch=ak_fetch,
            baostock_fetch=bs_fetch,
            threshold_bps=threshold_bps,
        )
        per_symbol.append(psr)

    duration = time.monotonic() - t0
    report = CrossSourceReport(
        target_date=target,
        started_at=started.isoformat(),
        duration_s=duration,
        threshold_bps=threshold_bps,
        per_symbol=per_symbol,
    )
    logger.info(
        "cross_source check date={d} symbols={n} passed={p} "
        "failed={f} skipped={s} duration={dur:.2f}s",
        d=target,
        n=len(per_symbol),
        p=report.n_passed,
        f=report.n_failed,
        s=report.n_skipped,
        dur=duration,
    )
    if notify_on_fail:
        _notify_cross_source_failures(report)
    return report
