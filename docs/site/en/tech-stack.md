# Tech Stack

CLAUDE.md says "already chosen, don't introduce alternatives". The table below lists every library the project actually uses + the rationale.

## Core stack

| Dimension | Choice | Version | Why |
| --- | --- | --- | --- |
| **Python** | 3.11+ | `requires-python` in pyproject | AKQuant + pandas 2.x + DuckDB 1.x all require 3.10+ |
| **Data (primary)** | akshare | ≥ 1.16.0 | Most complete A-share coverage, AKQuant-ecosystem compatible |
| **Data (validator)** | baostock | ≥ 0.9.3 | Cross-source validation (akshare direct occasionally hits proxy issues) |
| **Data (alt)** | adata, tushare | ≥ 1.0.0, ≥ 1.4.0 | Fallback sources + realtime quotes |
| **Storage** | DuckDB | ≥ 1.0.0 | Single-file OLAP, `(symbol, date)` PK for idempotent upsert |
| **Storage** | Parquet (pyarrow) | ≥ 17.0.0 | `data/raw` + `data/clean` two layers, df.attrs metadata round-trip |
| **Engine** | AKQuant | ≥ 0.1.0 | Final choice after 17-framework survey; no source modifications |
| **Data processing** | pandas | ≥ 2.2.0 | Factor library uses pandas (not AKQuant polars DSL — keeps debuggability) |
| **Numerics** | numpy | ≥ 1.26.0 | Factor / performance base |
| **Numerics** | scipy | ≥ 1.13.0 | Minor use in walk-forward |
| **Optimization** | optuna | ≥ 4.0.0 | **CLAUDE.md explicitly bans grid search** |
| **Fast scanning (optional)** | vectorbt | ≥ 0.26.0 | Big-data backtest acceleration (optional) |
| **Visualization** | Streamlit | ≥ 1.38.0 | Dashboard |
| **Visualization** | Plotly | ≥ 5.22.0 | Dashboard charts |
| **Scheduling** | APScheduler | ≥ 3.10.0 | daily 18:00 ingest + Sunday 9:00 paper cron |
| **Logging** | loguru | ≥ 0.7.0 | Project-wide |
| **Alerts** | DingTalk webhook (via `requests`) | ≥ 2.32.0 | env-gated (`DINGTALK_WEBHOOK_URL`), default inactive |
| **Testing** | pytest | ≥ 8.0.0 | 537 tests, ~30s full suite |
| **Coverage** | pytest-cov | ≥ 5.0.0 | optional |
| **Lint** | ruff | ≥ 0.6.0 | Single tool replacing flake8 + isort + black |
| **Types** | mypy | ≥ 1.10.0 | strict mode, 0 errors across all modules |

## Docs site (project-internal, not a runtime dep)

| Tool | Version | Purpose |
| --- | --- | --- |
| mkdocs | ≥ 1.6.0 | static site generator |
| mkdocs-material | ≥ 9.5.0 | Material theme + Chinese support |
| mkdocstrings[python] | ≥ 0.25.0 | auto API docs |
| pymdown-extensions | ≥ 10.0.0 | mermaid / tabbed / details / emoji |

Install with `uv sync --extra docs`.

## Stacks deliberately *not* used

CLAUDE.md says "don't suggest new deps unless necessary with comparison". The following commonly-used stacks are explicitly excluded:

| Category | Excluded | Reason |
| --- | --- | --- |
| **Backtest engine** | backtrader | Upstream stalled (no commits after 2024-08); all A-share rules need self-patching |
| **Backtest engine** | vectorbt | Good for fast scan but unfriendly to A-share (downgraded to optional) |
| **Backtest engine** | QUANTAXIS / NautilusTrader | Heavy platforms, maintenance cost + black-box risk |
| **Backtest engine** | Lean | Unfriendly to A-share |
| **ML framework** | PyTorch / TensorFlow | CLAUDE.md explicitly says "don't introduce for ML" |
| **Visualization** | Flask + Echarts | CLAUDE.md explicitly bans in favor of "Streamlit + Plotly" |
| **Scheduling** | cron | CLAUDE.md "APScheduler" (cron unreliable on Windows) |
| **Logging** | stdlib `logging` | CLAUDE.md "loguru" |
| **Alerting** | PagerDuty | "personal project, don't use" |
| **Testing** | unittest | "pytest" |

## Dev-only

| Tool | Version |
| --- | --- |
| `mypy` | ≥ 1.10.0 (strict mode) |
| `ruff` | ≥ 0.6.0 (linter + formatter) |
| `pytest-cov` | ≥ 5.0.0 |

CI runs `uv run ruff check .` + `uv run mkdocs build --strict` in `.github/workflows/docs.yml`.