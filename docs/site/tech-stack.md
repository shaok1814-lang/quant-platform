# 技术栈

CLAUDE.md 明确「已选定，不要引入其他方案」。下表列出项目实际使用的所有库包 + 选择理由。

## 核心栈

| 维度 | 选型 | 版本约束 | 选型理由 |
| --- | --- | --- | --- |
| **Python** | 3.11+ | pyproject `requires-python` | AKQuant + pandas 2.x + DuckDB 1.x 都要求 3.10+ |
| **数据 (主)** | akshare | ≥ 1.16.0 | A 股数据覆盖最全，与 AKQuant 生态兼容 |
| **数据 (校验)** | baostock | ≥ 0.9.3 | akshare ↔ baostock 跨源校验（akshare 直接  偶发代理问题） |
| **数据 (备选)** | adata, tushare | ≥ 1.0.0, ≥ 1.4.0 | 备用源 + 实时报价 |
| **存储** | DuckDB | ≥ 1.0.0 | 单文件 OLAP，`(symbol, date)` PK 幂等 upsert |
| **存储** | Parquet (pyarrow) | ≥ 17.0.0 | `data/raw` + `data/clean` 双层，df.attrs 元数据 round-trip |
| **引擎** | AKQuant | ≥ 0.1.0 | 选型调研 17 框架后的最终决定；不修改源码 |
| **数据处理** | pandas | ≥ 2.2.0 | 因子库统一 pandas（不用 AKQuant polars DSL — 保留可调试性） |
| **数值计算** | numpy | ≥ 1.26.0 | 因子 / 绩效计算基础 |
| **数值计算** | scipy | ≥ 1.13.0 | walk-forward 中少量需要 |
| **参数优化** | optuna | ≥ 4.0.0 | **CLAUDE.md 明确禁止 grid search** |
| **快速扫描 (可选)** | vectorbt | ≥ 0.26.0 | 大数据量回测加速（**可选项**） |
| **可视化** | Streamlit | ≥ 1.38.0 | Dashboard |
| **可视化** | Plotly | ≥ 5.22.0 | Dashboard 图表 |
| **调度** | APScheduler | ≥ 3.10.0 | daily 18:00 ingest + Sunday 9:00 paper cron |
| **日志** | loguru | ≥ 0.7.0 | 全项目统一 |
| **告警** | 钉钉 webhook (via `requests`) | ≥ 2.32.0 | env-var gated (`DINGTALK_WEBHOOK_URL`)，默认 inactive |
| **测试** | pytest | ≥ 8.0.0 | 537 个测试，~30s 全套 |
| **覆盖率** | pytest-cov | ≥ 5.0.0 | 可选 |
| **Lint** | ruff | ≥ 0.6.0 | 单工具替代 flake8 + isort + black |
| **类型** | mypy | ≥ 1.10.0 | strict 模式，全模块 0 errors |

## 文档站（项目自身用，不计入运行时依赖）

| 工具 | 版本 | 用途 |
| --- | --- | --- |
| mkdocs | ≥ 1.6.0 | 静态站点生成 |
| mkdocs-material | ≥ 9.5.0 | Material theme + 中文支持 |
| mkdocstrings[python] | ≥ 0.25.0 | 自动 API 文档 |
| pymdown-extensions | ≥ 10.0.0 | mermaid / tabbed / details / emoji |

通过 `uv sync --extra docs` 安装。

## 故意 *不* 使用的栈

CLAUDE.md 「不要主动建议引入新的依赖，除非确实必要且给出对比理由」。以下常见栈被明确排除：

| 类别 | 排除 | 理由 |
| --- | --- | --- |
| **回测引擎** | backtrader | 上游停滞（2024-08 后无提交）；A 股规则全需自补 |
| **回测引擎** | vectorbt | 适合快速扫描但对 A 股不友好（弃用可选依赖） |
| **回测引擎** | QUANTAXIS / NautilusTrader | 重型平台，维护成本 + 黑盒风险 |
| **回测引擎** | Lean | 不友好于 A 股 |
| **ML 框架** | PyTorch / TensorFlow | CLAUDE.md 明确「不要为了 ML 引入」 |
| **可视化** | Flask + Echarts | CLAUDE.md 明确禁止「Streamlit + Plotly」 |
| **调度** | cron | CLAUDE.md 「APScheduler」(Windows 不可靠) |
| **日志** | 标准 logging | CLAUDE.md 「loguru」 |
| **告警** | PagerDuty | 「个人项目不要接」 |
| **测试** | unittest | 「pytest」 |

## Dev-only

| 工具 | 版本 |
| --- | --- |
| mypy | ≥ 1.10.0 (strict mode) |
| ruff | ≥ 0.6.0 (linter + formatter) |
| pytest-cov | ≥ 5.0.0 |

CI 在 `.github/workflows/docs.yml` 跑 `uv run ruff check .` + `uv run mkdocs build --strict`。