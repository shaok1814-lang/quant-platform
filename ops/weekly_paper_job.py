"""Weekly paper-validation job (W6.5).

Runs a single-symbol paper session on a fixed schedule (Sunday 9:00
Asia/Shanghai by default). Wired into :func:`ops.scheduler.build_scheduler`
as the second registered job.

The point is to enforce CLAUDE.md 「实盘前必须经过至少 4 周模拟盘验证」:
every Sunday, a fresh paper session is recorded and the operator can
see a 4-week trailing equity curve by inspecting
``data/paper_reports/weekly_<date>.json`` (W6.2 dashboard Phase 2 will
graph these in the same way the AKQuant backtest NAVs are rendered).

What the job does (in order):

  1. Load the last ``lookback_days`` (default 60 trading days) of
     OHLCV for ``symbol`` (default ``000001``) from DuckDB via
     :class:`data_layer.storage.duck.DuckStore`.
  2. Wrap :class:`research.strategies.ma_cross.MACrossStrategy` (the
     W1 baseline strategy + per [[ma-cross-baseline-000001-20240826]]
     anchor) in :class:`execution.bridge.AkquantStrategyCallable`.
  3. Run :func:`execution.run_paper_session` with
     :class:`execution.AkquantPaperAdapter` and a fresh
     :class:`execution.PaperJournal` (per-rotation SQLite file so
     4 weeks of reports can coexist without conflict).
  4. Write a :class:`WeeklyPaperReport` JSON to
     ``data/paper_reports/weekly_<date>.json``. Schema is
     dashboard-friendly (W6.2 Phase 2 will read this).
  5. Fire 钉聊 alert if the drawdown kill switch fired during the
     session — same body format as W7.1 Phase 3's runner alert.

**Backward compat**: the only strategy supported out of the box is
``MACrossStrategy`` (the W1 baseline). Other strategies can be
plugged in by passing ``strategy_cls=`` to
:func:`run_weekly_paper_session` (used by tests; production keeps
the baseline for simplicity — single-strategy validation is the
whole point of the weekly cycle).

**Dev / CI**: zero network, zero xtquant. All DuckDB / strategy / journal
knobs are injectable so unit tests can run with stub data.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from datetime import date as date_cls
from pathlib import Path
from typing import Any, Final

from data_layer.storage.duck import DuckStore
from execution.protocol import DEFAULT_RISK_CONFIG
from loguru import logger

__all__ = [
    "DEFAULT_DUCKDB_PATH",
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_OUTPUT_DIR",
    "DEFAULT_SYMBOL",
    "WeeklyPaperReport",
    "run_weekly_paper_session",
]

# Default DuckDB path mirrors W2.1's production file.
_PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_DUCKDB_PATH: Final[Path] = _PROJECT_ROOT / "data" / "duckdb" / "daily.duckdb"

# Lookback: 60 trading days ≈ 12 weeks of daily bars. Wide enough for
# the W5 walk-forward in-sample window; narrow enough that
# ``run_paper_session`` finishes in <30s on a single symbol.
DEFAULT_LOOKBACK_DAYS: Final[int] = 60

# Default symbol: 000001 — the [[ma-cross-baseline-000001-20240826]]
# anchor and the W1 baseline backtest target. Single-strategy
# validation is the point of the weekly cycle; multi-symbol comes
# after Phase 5.
DEFAULT_SYMBOL: Final[str] = "000001"

# Default output: ``data/paper_reports/`` (mirrors the layout used by
# the dashboard — same root, easy to point Streamlit at).
DEFAULT_OUTPUT_DIR: Final[Path] = _PROJECT_ROOT / "data" / "paper_reports"

# Default strategy class is imported lazily (AKQuant may not be
# installed everywhere; importing at module load would force it).
DEFAULT_STRATEGY_CLS_PATH: Final[str] = "research.strategies.ma_cross.MACrossStrategy"


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WeeklyPaperReport:
    """Aggregate outcome of one weekly paper-validation session.

    Attributes:
        run_date: UTC date when the job was triggered.
        symbol: Symbol that was traded.
        start_date: First trading day in the loaded window.
        end_date: Last trading day in the loaded window.
        n_bars: Number of bars the runner drove (excludes bars with
            no strategy action — but since the runner iterates
            every bar, this is the same as ``end_date - start_date + 1``
            in trading days).
        started_at: UTC ISO timestamp when the job started.
        duration_s: Wall-clock duration.
        n_intents: Total OrderIntents emitted by the strategy.
        n_risk_rejected: Risk-rejected intents.
        n_filled: Filled intents.
        final_equity: Total equity at session end.
        max_drawdown_pct: Maximum drawdown observed (session-local).
        kill_switch_fired: Whether the drawdown kill switch flipped
            0→1 during the session (drives the 钉聊 alert).
        report_path: Absolute path of the JSON file written (filled
            by the runner post-write; useful for the dashboard).
    """

    run_date: date_cls
    symbol: str
    start_date: date_cls
    end_date: date_cls
    n_bars: int
    started_at: str
    duration_s: float
    n_intents: int
    n_risk_rejected: int
    n_filled: int
    final_equity: float
    max_drawdown_pct: float
    kill_switch_fired: bool
    report_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Render as a JSON-serializable dict (dashboard-friendly)."""
        d = asdict(self)
        d["run_date"] = self.run_date.isoformat()
        d["start_date"] = self.start_date.isoformat()
        d["end_date"] = self.end_date.isoformat()
        return d


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_weekly_paper_session(
    *,
    symbol: str = DEFAULT_SYMBOL,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    duckdb_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    strategy_cls: type | None = None,
    notify_on_kill_switch: bool = True,
) -> WeeklyPaperReport:
    """Run the weekly paper-validation session end-to-end.

    Args:
        symbol: 6-digit symbol to trade. Default ``"000001"`` (W1
            baseline anchor).
        lookback_days: How many calendar days of history to load.
            Default 60. The DuckDB query converts this into a
            ``start_date / end_date`` pair via
            :func:`_window_bounds`.
        duckdb_path: Path to the DuckDB file. Defaults to
            :data:`DEFAULT_DUCKDB_PATH` (production layout).
        output_dir: Where to write the JSON report. Defaults to
            :data:`DEFAULT_OUTPUT_DIR` (production layout).
        strategy_cls: Override the strategy class (default
            ``research.strategies.ma_cross.MACrossStrategy``). Pass
            any class that satisfies the AKQuant ``Strategy`` contract.
            Tests pass a stub strategy to avoid the AKQuant import.
        notify_on_kill_switch: If ``True`` (default), 钉聊 alert when
            the drawdown kill switch fires. Set ``False`` in tests.

    Returns:
        :class:`WeeklyPaperReport` with all session metrics + the
        path of the written JSON file.

    Raises:
        FileNotFoundError: DuckDB file does not exist.
        ValueError: DuckDB has no rows for ``symbol`` in the window.
    """
    started = datetime.now(UTC)
    t0 = time.monotonic()
    db_path = Path(duckdb_path) if duckdb_path is not None else DEFAULT_DUCKDB_PATH
    if not db_path.exists():
        raise FileNotFoundError(f"DuckDB not found: {db_path}")

    out_dir = Path(output_dir) if output_dir is not None else DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # Window: last ``lookback_days`` calendar days. Use calendar
    # days rather than business days — DuckDB's window is calendar
    # based (``date <=``), and ~14 calendar days of weekend
    # padding gives us a comfortable 60 trading days.
    end_date = started.date()
    start_date = end_date - timedelta(days=lookback_days)

    # Load OHLCV from DuckDB.
    with DuckStore(db_path) as store:
        df = store.query_daily_bars(
            symbol=symbol,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )
    if df.empty:
        raise ValueError(
            f"no rows for {symbol} in {start_date}..{end_date}; "
            f"check DuckDB ingestion for this symbol."
        )

    # Build strategy + bridge. Lazy-imports keep this module
    # importable without AKQuant / research dependencies (test stubs).
    bridge = _build_bridge(strategy_cls, symbol=symbol)

    # Per-rotation journal so multiple weeks coexist. Path:
    # ``data/paper_reports/journal_<date>.sqlite``.
    from execution import PaperJournal, run_paper_session

    # AkquantPaperAdapter is lazy-imported via execution.__getattr__
    # (heavy AKQuant dependency); import it directly here so mypy
    # sees the concrete class type instead of ``object``.
    from execution.brokers.akquant_paper import AkquantPaperAdapter

    journal_path = out_dir / f"journal_{started.date().isoformat()}.sqlite"
    adapter = AkquantPaperAdapter()
    journal = PaperJournal(journal_path)
    # Wire in-session kill-switch alerts to 钉聊 so the operator sees
    # intra-session drawdown flips (not just post-session). Mirrors
    # the post-session _notify_kill_switch path below.
    from execution.runner import PaperSessionConfig

    session_cfg = (
        PaperSessionConfig(notify_fn=lambda t, b: notify.ding(t, b))
        if notify_on_kill_switch
        else None
    )
    # Paper-mode RiskConfig: relax position_cap to match backtest intent
    # (95%). The strategy's TARGET_PERCENT (0.95) is the research-grade
    # deploy size; the default 10% cap from ``DEFAULT_RISK_CONFIG`` would
    # reject every order intent (risk_cap > strategy_target → no fills,
    # no P&L, no kill-switch test data). Live deployment is a separate
    # question — there the 10% cap from CLAUDE.md applies.
    paper_risk_cfg = replace(DEFAULT_RISK_CONFIG, max_position_pct=0.95)

    try:
        paper_report = run_paper_session(
            strategy=bridge,
            data=df,
            adapter=adapter,
            journal=journal,
            session_cfg=session_cfg,
            risk_cfg=paper_risk_cfg,
        )
    except Exception as exc:
        logger.exception("weekly paper session crashed")
        if notify_on_kill_switch:
            notify.ding(
                f"Weekly paper CRASHED ({symbol})",
                f"run_date={started.date()}\nerror={type(exc).__name__}: {exc}",
            )
        raise

    # Kill-switch detection uses the adapter's LIFETIME drawdown
    # (the runner checks ``adapter.query_account().drawdown_pct``
    # against the kill cap on each bar; ``paper_report.max_drawdown_pct``
    # is the SESSION-local drawdown, which is 0% in cost-basis
    # paper mode even when the lifetime drawdown exceeds the cap).
    final_snap = adapter.query_account()
    kill_switch_fired = final_snap.drawdown_pct >= 0.05
    report_path = out_dir / f"weekly_{started.date().isoformat()}.json"
    weekly = WeeklyPaperReport(
        run_date=started.date(),
        symbol=symbol,
        start_date=df["date"].min().date(),
        end_date=df["date"].max().date(),
        n_bars=len(df),
        started_at=started.isoformat(),
        duration_s=time.monotonic() - t0,
        n_intents=paper_report.n_intents,
        n_risk_rejected=paper_report.n_risk_rejected,
        n_filled=paper_report.n_filled,
        final_equity=paper_report.final_equity,
        max_drawdown_pct=paper_report.max_drawdown_pct,
        kill_switch_fired=kill_switch_fired,
        report_path=str(report_path),
    )
    report_path.write_text(
        json.dumps(weekly.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    logger.info(
        "weekly paper done symbol={sym} bars={b} "
        "intents={i} filled={f} dd={dd:.2%} "
        "kill_switch={ks} report={p}",
        sym=symbol,
        b=len(df),
        i=paper_report.n_intents,
        f=paper_report.n_filled,
        dd=paper_report.max_drawdown_pct,
        ks=kill_switch_fired,
        p=report_path,
    )

    if notify_on_kill_switch and kill_switch_fired:
        _notify_kill_switch(weekly)

    return weekly


def _build_bridge(strategy_cls: type | None, *, symbol: str) -> Any:
    """Construct the AKQuant strategy bridge.

    Lazy-imports the bridge module so tests that pass a stub
    ``strategy_cls`` don't pay the AKQuant import cost.
    """
    from execution.bridge import AkquantStrategyCallable

    if strategy_cls is None:
        # Default: MACrossStrategy (the W1 baseline).
        import importlib

        module_path, _, attr = DEFAULT_STRATEGY_CLS_PATH.rpartition(".")
        strategy_cls = getattr(importlib.import_module(module_path), attr)
    return AkquantStrategyCallable(strategy_cls, symbol=symbol)


def _notify_kill_switch(weekly: WeeklyPaperReport) -> None:
    """Send a 钉聊 alert when the weekly run hit the kill switch.

    Body format is stable (Phase 5 parsers can extract via regex):
    plain text, one field per line. Mirrors W7.1 Phase 3's
    ``format_kill_switch_body`` but at the weekly-cycle granularity.
    """
    from ops import notify

    body = (
        f"weekly paper run hit drawdown kill switch\n"
        f"run_date={weekly.run_date}\n"
        f"symbol={weekly.symbol}\n"
        f"window={weekly.start_date}..{weekly.end_date}\n"
        f"final_equity={weekly.final_equity:.0f}\n"
        f"max_drawdown_pct={weekly.max_drawdown_pct:.2%}\n"
        f"n_filled={weekly.n_filled}\n"
        f"report={weekly.report_path}"
    )
    notify.ding(f"Weekly paper kill switch ({weekly.symbol})", body)
