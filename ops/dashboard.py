"""W6.2.2 + W6.2.3 Streamlit dashboard — entry point.

Launch::

    streamlit run ops/dashboard.py

Pages:

  * **Page 1 — Universe Status** — ``ops.dashboard_data.load_universe_status``
    rendered as a table; the user sees row counts / last_dt / fetcher
    distribution per symbol. Helps spot ETFs that baostock rejected
    (4 symbols with 0 rows post-W6.1 first ingest).

  * **Page 2 — Equity Curves** — pick a strategy + one or more symbols;
    per-symbol AKQuant backtests run on the loaded DuckDB bars and
    the resulting NAV curves are plotted together on one chart. A
    small stats table shows per-symbol Sharpe / Sortino / MDD.

  * **Page 3 — Paper Trade History** (W6.2.3) — list of weekly paper
    runs (one row per Sunday's :func:`ops.weekly_paper_job.run_weekly_paper_session`).
    Pick a run → fills + intents of that run below.

Design choices:

  * **Streamlit is a thin shell**. All data loading + backtest
    wrappers live in :mod:`ops.dashboard_data` so they can be
    unit-tested WITHOUT the Streamlit runtime. This file is
    only responsible for the UI: ``st.title`` / ``st.dataframe``
    / ``st.plotly_chart`` / form widgets.

  * **Per-symbol backtest** (not universe-wide) because AKQuant's
    ``run_backtest`` on a multi-symbol universe yields ONE portfolio
    equity curve — the per-symbol contribution view needs a
    single-symbol backtest per row, which is more useful for the
    dashboard's strategy-exploration purpose. Universe-mode is a
    later task (W6.2 B-Chunk 2).

  * **Cached via ``st.cache_data``** so the user can flip pages
    without AKQuant re-running the whole backtest on each rerun.

  * **No file mutations**. The dashboard is read-only; it does not
    call ``ops.ingest_job.run_daily_ingest`` etc. (Scheduler is
    the only mutator; this UI is a viewer.)
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ops.dashboard_data import (
    DEFAULT_DUCKDB_PATH,
    DEFAULT_PAPER_DIR,
    compute_strategy_equity,
    load_paper_fills,
    load_paper_intents,
    load_paper_run_summaries,
    load_universe_status,
)

# ---------------------------------------------------------------------------
# Page config + nav
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="A 股量化 dashboard",
    page_icon=":bar_chart:",
    layout="wide",
)

PAGES: dict[str, str] = {
    "Universe Status": ":bar_chart: universe",
    "Equity Curves": ":chart_with_upwards_trend: equity curves",
    "Paper Trade History": ":memo: paper trade history",
}


def _page_selector() -> str:
    """Streamlit sidebar page selector (returns the chosen page name)."""
    with st.sidebar:
        st.title("W6.2 dashboard")
        st.caption(
            f"DuckDB: {DEFAULT_DUCKDB_PATH.relative_to(Path.cwd()) if DEFAULT_DUCKDB_PATH.is_relative_to(Path.cwd()) else DEFAULT_DUCKDB_PATH}"
        )
        return st.radio("page", list(PAGES), label_visibility="collapsed")


# ---------------------------------------------------------------------------
# Page 1 — Universe Status
# ---------------------------------------------------------------------------


def render_universe_status() -> None:
    st.header("Universe Status")
    st.caption(
        "Per-symbol bar counts and coverage in ``data/duckdb/daily.duckdb``. "
        "Sorted by symbol; ``n_trading_days`` is the business-day count between "
        "first and last bar (gap detector)."
    )

    status = load_universe_status()
    if status.empty:
        st.warning(
            "DuckDB has no bars yet. Run ``ops.ingest_job.ingest_window`` "
            "manually, or wait for the 18:00 daily scheduler."
        )
        return

    # Make dates display as YYYY-MM-DD strings (DuckDB returns date).
    status_display = status.copy()
    status_display["first_dt"] = status_display["first_dt"].astype(str)
    status_display["last_dt"] = status_display["last_dt"].astype(str)

    # Layout: KPI tiles + full table.
    cols = st.columns(4)
    with cols[0]:
        st.metric("symbols", len(status_display))
    with cols[1]:
        st.metric("total bars", int(status_display["n_rows"].sum()))
    with cols[2]:
        st.metric(
            "earliest",
            str(status_display["first_dt"].min()),
        )
    with cols[3]:
        st.metric(
            "latest",
            str(status_display["last_dt"].max()),
        )

    st.dataframe(
        status_display,
        use_container_width=True,
        hide_index=True,
    )

    # Gap detection: rows where n_rows << n_trading_days are
    # candidates for missing bars (stale fetcher / holiday glarce).
    status_display["gap"] = status_display["n_trading_days"] - status_display["n_rows"]
    gap_heavy = status_display[status_display["gap"] > 5]
    if not gap_heavy.empty:
        st.warning(
            f"{len(gap_heavy)} symbols have > 5 missing bars: "
            f"{sorted(gap_heavy['symbol'].tolist())}"
        )


# ---------------------------------------------------------------------------
# Page 2 — Equity Curves
# ---------------------------------------------------------------------------


# Strategy registry. Human label → canonical class name. The class
# import is lazy (see :func:`_get_strategy_class`) so ``streamlit run``
# on a freshly-opened container doesn't pull in AKQuant before any page
# is actually viewed.
STRATEGY_REGISTRY: dict[str, str] = {
    "MA Cross (single)": "MACrossStrategy",
    "Factor Timing MA Cross (single)": "FactorTimingMACross",
    "TopN Mean Reversion (multi)": "TopNMeanReversionStrategy",
}


def _get_strategy_class(canonical_name: str) -> type:
    """Lazy import of a strategy class by its canonical AKQuant name.

    Keeps the dashboard import-time light (no AKQuant / numpy pulled
    until the user clicks "Equity Curves" and a strategy).
    """
    if canonical_name == "MACrossStrategy":
        from research.strategies.ma_cross import MACrossStrategy

        return MACrossStrategy
    if canonical_name == "FactorTimingMACross":
        from research.strategies.factor_timing import FactorTimingMACross

        return FactorTimingMACross
    if canonical_name == "TopNMeanReversionStrategy":
        from research.strategies.topn_mean_reversion import TopNMeanReversionStrategy

        return TopNMeanReversionStrategy
    raise ValueError(f"unknown strategy: {canonical_name}")


@st.cache_data(show_spinner=False)
def _run_per_symbol_backtest(
    strategy_name: str,
    symbols: tuple[str, ...],
    start_date: str | None,
    end_date: str | None,
    initial_cash: float,
) -> dict[str, pd.Series]:
    """Per-symbol AKQuant backtest. Cached on the input tuple so
    flipping Streamlit widgets doesn't re-run if the user didn't
    change them.

    Returns ``{symbol: equity_curve_series}`` for symbols where the
    backtest produced any equity points. Symbols with empty DuckDB
    data or zero trades are omitted (UI shows the per-symbol table
    with the omission explained).
    """
    from ops.dashboard_data import load_symbol_bars

    strategy_cls = _get_strategy_class(strategy_name)
    out: dict[str, pd.Series] = {}
    progress = st.progress(0.0, text="loading bars…")
    for i, sym in enumerate(symbols, start=1):
        bars = load_symbol_bars(sym, start_date=start_date, end_date=end_date)
        if bars.empty:
            continue
        progress.progress(i / len(symbols), text=f"backtesting {sym}…")
        equity, _result = compute_strategy_equity(
            strategy_cls,
            data=bars,
            initial_cash=initial_cash,
        )
        if equity is None or len(equity) == 0:
            continue
        out[sym] = equity
    progress.empty()
    return out


def _series_to_dataframe(per_symbol_equity: dict[str, pd.Series]) -> pd.DataFrame:
    """Stack per-symbol equity series into a single DataFrame
    aligned on their union of timestamps. AKQuant's equity series
    may have different timezones / indices per symbol after a
    single-symbol backtest — we let pandas align them.
    """
    pieces: list[pd.DataFrame] = []
    for sym, series in per_symbol_equity.items():
        s = series.copy()
        s.name = sym
        pieces.append(s)
    return pd.concat(pieces, axis=1).sort_index()


def _summarize_per_symbol(per_symbol_equity: dict[str, pd.Series]) -> pd.DataFrame:
    """One-row-per-symbol summary: total return, max drawdown."""
    rows = []
    for sym, equity in per_symbol_equity.items():
        if len(equity) == 0:
            continue
        start = float(equity.iloc[0])
        end = float(equity.iloc[-1])
        total_ret = (end / start) - 1.0 if start else 0.0
        running_max = equity.cummax()
        mdd = float(((equity - running_max) / running_max).min())
        rows.append(
            {
                "symbol": sym,
                "start": start,
                "end": end,
                "total_return": total_ret,
                "max_drawdown": mdd,
            }
        )
    return pd.DataFrame(rows).sort_values("total_return", ascending=False)


def render_equity_curves() -> None:
    st.header("Equity Curves")
    st.caption(
        "Per-symbol backtest. AKQuant runs the chosen strategy on each "
        "selected symbol independently; NAV curves are aligned on the "
        "common date index."
    )

    universe = load_universe_status()
    if universe.empty:
        st.warning("No data. Run the ingest first.")
        return

    available_symbols = universe["symbol"].tolist()

    strategy_label = st.selectbox(
        "strategy",
        list(STRATEGY_REGISTRY.keys()),
    )
    selected = st.multiselect(
        "symbols (multi-select; 1-10 keeps backtest < 30s)",
        available_symbols,
        default=available_symbols[:3] if len(available_symbols) >= 3 else available_symbols,
        max_selections=10,
    )
    col1, col2, col3 = st.columns(3)
    with col1:
        start_date = st.text_input(
            "start_date (YYYY-MM-DD, optional)",
            value="",
            placeholder="2025-06-15",
        )
    with col2:
        end_date = st.text_input(
            "end_date (YYYY-MM-DD, optional)",
            value="",
            placeholder="2026-08-28",
        )
    with col3:
        initial_cash = st.number_input(
            "initial cash (CNY)",
            min_value=10_000,
            max_value=100_000_000,
            value=1_000_000,
            step=100_000,
        )

    if not selected:
        st.info("pick at least one symbol")
        return

    canonical_name = STRATEGY_REGISTRY[strategy_label]

    start = start_date or None
    end = end_date or None

    with st.spinner("running per-symbol AKQuant backtests…"):
        per_symbol = _run_per_symbol_backtest(
            canonical_name,
            tuple(selected),
            start,
            end,
            float(initial_cash),
        )

    if not per_symbol:
        st.warning(
            "No symbols produced trades. The strategy may need more "
            "bars, or the date range is too narrow."
        )
        return

    df = _series_to_dataframe(per_symbol)

    # Plotly multi-line chart.
    fig = go.Figure()
    for sym in df.columns:
        fig.add_trace(
            go.Scatter(
                x=df.index,
                y=df[sym],
                mode="lines",
                name=sym,
            )
        )
    fig.update_layout(
        title="NAV (per-symbol, single-position)",
        xaxis_title="date",
        yaxis_title="CNY (start = initial_cash)",
        hovermode="x unified",
        height=480,
    )
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("per-symbol summary")
    summary = _summarize_per_symbol(per_symbol)
    summary_display = summary.copy()
    summary_display["total_return"] = summary_display["total_return"].map(lambda v: f"{v:.2%}")
    summary_display["max_drawdown"] = summary_display["max_drawdown"].map(lambda v: f"{v:.2%}")
    summary_display["start"] = summary_display["start"].map(lambda v: f"{v:,.0f}")
    summary_display["end"] = summary_display["end"].map(lambda v: f"{v:,.0f}")
    st.dataframe(summary_display, use_container_width=True, hide_index=True)

    st.caption(
        f"backtested {len(per_symbol)} / {len(selected)} symbols successfully "
        f"(strategy={strategy_label}, initial_cash={initial_cash:,.0f} CNY)"
    )


# ---------------------------------------------------------------------------
# Page 3 — Paper Trade History (W6.2.3)
# ---------------------------------------------------------------------------


def _journal_path_for_run(reports_dir: Path, run_date_iso: str) -> Path:
    """The weekly paper job writes a journal SQLite per run, named
    ``journal_<YYYY-MM-DD>.sqlite``. Same directory as the JSON."""
    return reports_dir / f"journal_{run_date_iso}.sqlite"


@st.cache_data(show_spinner=False)
def _load_runs(reports_dir_str: str) -> pd.DataFrame:
    """Cached: re-reads the directory only when the user re-runs the
    page (Streamlit invalidates on widget change)."""
    return load_paper_run_summaries(Path(reports_dir_str))


def render_paper_trade_history() -> None:
    st.header("Paper Trade History")
    st.caption(
        "Per-weekly paper-validation runs (W6.5 cron: every Sunday "
        "9:00 Asia/Shanghai). Pick a run to drill into its fills "
        "+ intents. Source: ``data/paper_reports/`` JSON + journal "
        "SQLite files written by ``ops.weekly_paper_job.run_weekly_paper_session``."
    )

    runs = _load_runs(str(DEFAULT_PAPER_DIR))
    if runs.empty:
        st.warning(
            "No weekly paper reports yet. The W6.5 cron will write the "
            "first JSON on the next Sunday 9:00 Asia/Shanghai. To "
            "produce one manually, run "
            "``ops.weekly_paper_job.run_weekly_paper_session``."
        )
        return

    # KPI tiles for the latest run (top of page).
    latest = runs.iloc[0]
    cols = st.columns(4)
    with cols[0]:
        st.metric("total runs", len(runs))
    with cols[1]:
        st.metric("latest run_date", str(latest["run_date"]))
    with cols[2]:
        st.metric(
            "latest final_equity",
            f"{float(latest['final_equity']):,.0f}",
        )
    with cols[3]:
        st.metric(
            "latest max_drawdown",
            f"{float(latest['max_drawdown_pct']):.2%}",
        )

    # Display table — make dates strings (Streamlit default formatter
    # is friendly on date objects but string avoids tz surprises).
    runs_display = runs.copy()
    for c in ("run_date", "start_date", "end_date"):
        runs_display[c] = runs_display[c].astype(str)
    st.dataframe(runs_display, use_container_width=True, hide_index=True)

    # Run selector for the drill-down. Use a synthetic label
    # ``YYYY-MM-DD | symbol | n_fills`` so the user sees the run
    # at a glance.
    def _label(idx: int) -> str:
        row = runs.iloc[idx]
        return (
            f"{row['run_date']} | {row['symbol']} | "
            f"fills={int(row['n_filled'])}"
        )

    selected = st.selectbox(
        "drill into",
        options=list(range(len(runs))),
        format_func=_label,
    )
    if selected is None:
        return
    run_row = runs.iloc[selected]
    run_date_iso = str(run_row["run_date"])
    journal_path = _journal_path_for_run(DEFAULT_PAPER_DIR, run_date_iso)

    # Fills sub-table.
    st.subheader(
        f"fills — {run_row['symbol']} | "
        f"{run_row['start_date']} → {run_row['end_date']}"
    )
    fills = load_paper_fills(journal_path)
    if fills.empty:
        st.info(
            "no fills in this run (or journal SQLite not found at "
            f"{journal_path}; check the W6.5 cron ran and wrote it)."
        )
    else:
        st.dataframe(fills, use_container_width=True, hide_index=True)

    # Intents sub-table (incl. risk_rejected) for the audit view.
    st.subheader(
        f"intents — {run_row['symbol']} | "
        f"{run_row['start_date']} → {run_row['end_date']}"
    )
    intents = load_paper_intents(journal_path)
    if intents.empty:
        st.info("no intents recorded")
    else:
        st.dataframe(intents, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


PAGE_RENDERERS = {
    "Universe Status": render_universe_status,
    "Equity Curves": render_equity_curves,
    "Paper Trade History": render_paper_trade_history,
}


def main() -> None:
    page = _page_selector()
    renderer = PAGE_RENDERERS[page]
    renderer()


if __name__ == "__main__":
    main()
