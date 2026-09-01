# Quick Start

Get your first backtest + paper run going in 5 minutes.

## Prerequisites

- **Python ≥ 3.11** (CLAUDE.md floor; CI runs on 3.12)
- **uv** — [install](https://docs.astral.sh/uv/getting-started/installation/)
- **Network** (first install pulls akshare / baostock; offline use cached parquet)

!!! note "Windows users"
    Live trading needs xtquant + miniQMT, Windows-only. Paper / backtest mode runs on any platform.

## 1. Install

```bash
git clone https://github.com/shaok1814-lang/quant-platform.git
cd quant-platform
uv sync --extra docs --frozen
```

`uv sync` creates `.venv/` and installs every dep in [pyproject.toml](https://github.com/shaok1814-lang/quant-platform/blob/master/pyproject.toml). `--extra docs` adds the 4 MkDocs packages (for this site).

## 2. Run tests (~30 seconds)

```bash
uv run pytest -q
```

Output similar to:

```text
................. [100%]
========== 537 passed, 3 skipped in 28.66s ==========
```

!!! tip "Markers"
- `pytest -m "not network"` — skip tests needing akshare / baostock
- `pytest -m "not slow"` — skip AKQuant run_backtest e2e

## 3. Run a backtest (~1 minute)

```bash
uv run python research/strategies/ma_cross.py
```

This is the W1 baseline — 5/20 SMA cross on `000001` via AKQuant. Outputs backtest metrics (Sharpe / Sortino / MDD / total return).

**DuckDB-driven variant** (same strategy but data flows through the data layer → parquet → DuckDB → backtest pipeline):

```bash
uv run python research/strategies/ma_cross_duckdb.py
```

## 4. Launch the dashboard (optional, needs DuckDB data)

```bash
# First time: populate DuckDB (single symbol, 60 days) + write one weekly report
uv run python scripts/run_paper_validation.py --smoke --weeks 1

# Launch the dashboard
uv run streamlit run ops/dashboard.py
```

Open http://localhost:8501 — 3-page dashboard:

- **Universe Status** — per-symbol DuckDB row count + gap detection
- **Equity Curves** — pick a strategy + symbols, Plotly NAV chart
- **Paper Trade History** — weekly paper runs + drill-down into fills + intents

!!! tip "No-network environment"
    `streamlit run ops/dashboard.py` doesn't need akshare. It only reads local DuckDB + parquet + weekly JSONs. With zero data the dashboard shows a friendly "No data yet".

## 5. Run the 4-week paper discipline (optional, long-running)

```bash
# One-shot pre-flight
uv run python scripts/run_paper_validation.py

# Launch scheduler (daily 18:00 ingest + Sunday 9:00 paper)
uv run python -m ops
```

See [Paper-Validation Runbook](paper-runbook.md) + `python scripts/run_paper_validation.py --help`.

## Common commands

```bash
# Lint + type check
uv run ruff check .
uv run mypy execution/ ops/ scripts/

# Run one weekly paper session manually
uv run python -c "from ops.weekly_paper_job import run_weekly_paper_session; print(run_weekly_paper_session())"

# Docs site local preview
uv run mkdocs serve    # http://127.0.0.1:8000/
```

Next steps: [Architecture](architecture.md) · [A-Share Rules](a-share-rules.md) · [Framework Survey](framework-survey.md).

---

## Next steps

- See how each module connects → [Architecture](architecture.md)
- 8 A-share rule details → [A-Share Rules](a-share-rules.md)
- How to run the 4-week paper discipline → [Paper-Validation Runbook](paper-runbook.md)
- 17-framework survey → [Framework Survey](framework-survey.md)