# API 参考

!!! abstract "用法"
    本页面是用 [mkdocstrings](https://mkdocstrings.github.io/) 从源码 docstrings 自动生成的。不再需要手动维护 — 改源码 docstring 后 `mkdocs build` 自动同步。

!!! note "排除的模块"
    - `execution.brokers.xtquant_live` — Windows-only，lazy-import xtquant，非 Windows 构建会失败。
    - `execution.brokers.xtquant_callbacks` / `xtquant_fake` / `xtquant_models` — 内部测试 driver + SDK 数据 shape，不属于公开 surface。
    - `ops.dashboard` — Streamlit 入口 (`streamlit run`)，不算 library API。

## 数据层

### `data_layer.storage.duck`

::: data_layer.storage.duck

### `data_layer.storage.parquet_io`

::: data_layer.storage.parquet_io

### `data_layer.validation.cross_source`

::: data_layer.validation.cross_source

### `data_layer.ingestion.akshare_fetcher`

::: data_layer.ingestion.akshare_fetcher

## 因子库

### `research.factor_lib.trend`

::: research.factor_lib.trend

### `research.factor_lib.momentum`

::: research.factor_lib.momentum

### `research.factor_lib.mean_reversion`

::: research.factor_lib.mean_reversion

### `research.factor_lib.liquidity`

::: research.factor_lib.liquidity

### `research.factor_lib.post`

::: research.factor_lib.post

### `research.factor_lib.pipeline`

::: research.factor_lib.pipeline

### `research.factor_lib.splits`

::: research.factor_lib.splits

### `research.factor_lib.analytics.performance`

::: research.factor_lib.analytics.performance

### `research.factor_lib.analytics.walk_forward`

::: research.factor_lib.analytics.walk_forward

### `research.factor_lib.analytics.param_sensitivity`

::: research.factor_lib.analytics.param_sensitivity

### `research.factor_lib.analytics.optuna_runner`

::: research.factor_lib.analytics.optuna_runner

## 策略

### `research.strategies.ma_cross`

::: research.strategies.ma_cross

### `research.strategies.factor_timing`

::: research.strategies.factor_timing

### `research.strategies.topn_mean_reversion`

::: research.strategies.topn_mean_reversion

### `research.strategies.donchian_breakout`

::: research.strategies.donchian_breakout

## A 股规则

### `backtest.a_share`

::: backtest.a_share

## 执行层

### `execution.protocol`

::: execution.protocol

### `execution.risk`

::: execution.risk

### `execution.runner`

::: execution.runner

### `execution.journal`

::: execution.journal

### `execution.brokers.akquant_paper`

::: execution.brokers.akquant_paper

### `execution.brokers.registry`

::: execution.brokers.registry

### `execution.bridge.akquant_strategy`

::: execution.bridge.akquant_strategy

## Ops

### `ops.universe`

::: ops.universe

### `ops.quality`

::: ops.quality

### `ops.notify`

::: ops.notify

### `ops.ingest_job`

::: ops.ingest_job

### `ops.weekly_paper_job`

::: ops.weekly_paper_job

### `ops.cross_source_job`

::: ops.cross_source_job

### `ops.scheduler`

!!! note "manual table — `build_scheduler` is documented in module docstring"
    See [project source](https://github.com/shaok1814-lang/quant-platform/blob/master/ops/scheduler.py) for `DEFAULT_HOUR` / `DEFAULT_MINUTE` / `DEFAULT_TZ` / `DEFAULT_WEEKLY_DAY_OF_WEEK` / `DEFAULT_WEEKLY_HOUR` / `DEFAULT_WEEKLY_MINUTE` constants and the `build_scheduler` signature.

::: ops.scheduler
    options:
      members:
        - build_scheduler

### `ops.dashboard_data`

::: ops.dashboard_data

## 下一步

- 想看这些模块怎么连起来 → [系统架构](architecture.md)
- 想跑测试 + 看 dashboard → [快速开始](quickstart.md)
- 想看怎么 4 周模拟盘 → [模拟盘手册](paper-runbook.md)
- 想看每个模块的 GitHub 源码 → [项目仓库](https://github.com/shaok1814-lang/quant-platform)