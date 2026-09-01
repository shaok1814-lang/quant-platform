# 快速开始

5 分钟跑通你的第一次回测 + 模拟盘。

## 前置条件

- **Python ≥ 3.11**（CLAUDE.md 设定的下限；CI 在 3.12 跑）
- **uv** — [安装](https://docs.astral.sh/uv/getting-started/installation/)
- **网络**（首次安装 akshare / baostock 数据源；离线环境可用预缓存的 parquet）

!!! note "Windows 用户"
    实盘需要 xtquant + miniQMT，仅 Windows。Paper / backtest 模式在任何平台跑。

## 1. 安装

```bash
git clone https://github.com/shaok1814-lang/quant-platform.git
cd quant-platform
uv sync --extra docs --frozen
```

`uv sync` 创建 `.venv/` 并安装 [pyproject.toml](https://github.com/shaok1814-lang/quant-platform/blob/master/pyproject.toml) 里的全部依赖。`--extra docs` 加上 MkDocs（本项目文档站所需的 4 个包）。

## 2. 跑测试（30 秒）

```bash
uv run pytest -q
```

输出类似：

```text
................. [100%]
========== 537 passed, 3 skipped in 28.66s ==========
```

!!! tip "标记筛选"
    - `pytest -m "not network"` — 跳过需要 akshare / baostock 网络的测试
    - `pytest -m "not slow"` — 跳过走 AKQuant run_backtest 的 e2e

## 3. 跑一次回测（1 分钟）

```bash
uv run python research/strategies/ma_cross.py
```

这是 W1 baseline — 5/20 SMA 交叉策略跑在 `000001` 上，AKQuant 引擎。结果输出 backtest metrics（Sharpe / Sortino / MDD / total return 等）。

**DuckDB 驱动版**（同样策略，走数据层 → parquet → DuckDB → backtest 全链路）：

```bash
uv run python research/strategies/ma_cross_duckdb.py
```

## 4. 启动 Dashboard（可选，需要 DuckDB 数据）

```bash
# 首次：写入一些 DuckDB 数据（单 symbol 60 天）
uv run python scripts/run_paper_validation.py --smoke --weeks 1

# 启动 dashboard
uv run streamlit run ops/dashboard.py
```

打开 http://localhost:8501 看到 3 页 dashboard：

- **Universe Status** — 每个 symbol 的 DuckDB 行数 + 覆盖率
- **Equity Curves** — 选策略 + 多 symbol，Plotly 画 NAV
- **Paper Trade History** — 每周 paper  paper 跑 + 的 weekly JSON + journal SQLite

!!! tip "无网络环境"
    `streamlit run ops/dashboard.py` 不需要 akshare。它只读本地 DuckDB + parquet + 已有 weekly JSON。如果完全无数据，dashboard 显示 friendly "No data yet"。

## 5. 跑 4 周模拟盘纪律（可选，长期）

```bash
# 一次性 pre-flight
uv run python scripts/run_paper_validation.py

# 启动 scheduler（daily 18:00 ingest + Sunday 9:00 paper）
uv run python -m ops
```

详见 [模拟盘手册](paper-runbook.md) + `python scripts/run_paper_validation.py --help`。

## 常用命令汇总

```bash
# Lint + 类型检查
uv run ruff check .
uv run mypy execution/ ops/ scripts/

# 跑一个 weekly paper session（手动）
uv run python -c "from ops.weekly_paper_job import run_weekly_paper_session; print(run_weekly_paper_session())"

# 文档站本地预览
uv run mkdocs serve    # http://127.0.0.1:8000/
```

下一步：[架构](architecture.md) · [A 股规则](a-share-rules.md) · [框架调研](framework-survey.md)。