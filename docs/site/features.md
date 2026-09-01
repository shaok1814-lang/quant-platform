# 功能一览

quant-platform 在 6 个层面都有可测试、有边界覆盖、可立即跑的实现。下表对应源代码目录 — 点击查看每个模块的详细文档。

<div class="teaser-grid" markdown>

<div class="teaser-card" markdown>
### [:material-database: 数据层](architecture.md#data-source)

**akshare (主) + baostock (校验)** → Parquet (raw/clean) → DuckDB (`daily_bars` 表，`(symbol, date)` 主键幂等 upsert)。每天 18:00 cron 增量入库，写入前过 6 项 HARD quality gate。

- :源: [`data_layer/`](https://github.com/shaok1814-lang/quant-platform/tree/master/data_layer)
- :测试: 30+ (data_layer / ingestion / parquet / DuckDB / quality)
- :依赖: akshare · baostock · adata · tushare · duckdb · pyarrow

</div>

<div class="teaser-card" markdown>
### [:material-function-variant: 因子库](architecture.md#factor-library)

**4 个 alpha 家族 + 3 个 cross-section 后处理器 + FactorPipeline 组合器**。pandas-only（不用 AKQuant polars DSL — 保留可调试性）。

| 家族 | 因子 | |
| | --- | --- |
| Trend | `ma_deviation(close, n=20)` | `(close - SMA) / SMA`，跨价位尺度不变 |
| Momentum | `n_day_return(close, n=20)` | 纯 N 日简单收益 |
| Mean-reversion | `rsi(close, n=14)`, `bollinger_z(close, n=20)` | Wilder RSI + 布林 z-score |
| Liquidity | `turnover_ratio(volume, outstanding)` | 成交量 / 总股本 |

后处理器：`winsorize`（3σ / MAD / 分位数）→ `standardize` → `Neutralizer`（行业中性化 hook）。

- :源: [`research/factor_lib/`](https://github.com/shaok1814-lang/quant-platform/tree/master/research/factor_lib)
- :测试: 40+ (4 家族 × 边界 + 后处理器 + pipeline)

</div>

<div class="teaser-card" markdown>
### [:material-strategy: 策略集](architecture.md#engine)

**5 个 AKQuant Strategy 子类**，覆盖 single-symbol 与 multi-symbol 两种模式：

| 策略 | 类别 | 描述 |
| --- | --- | --- |
| `MACrossStrategy` | single, trend-following | 5/20 SMA 交叉，W1 baseline |
| `MACrossStrategy` (DuckDB driver) | single | 同上但走 DuckDB 数据端到端 |
| `TopNMeanReversionStrategy` | multi, cross-section | 周调仓，RSI + 布林 z 双因子打分排序 |
| `FactorTimingMACross` | single, factor-as-filter | 5/20 cross + 动量因子做 gate（防假金叉） |
| `DonchianBreakoutStrategy` | single, breakout | Turtle S1 简化版，20-day high 入场、10-day low 出场 |

- :源: [`research/strategies/`](https://github.com/shaok1814-lang/quant-platform/tree/master/research/strategies)
- :测试: 9 + 10 + 14 + 9 (4 个 strategy e2e + 桥接)

</div>

<div class="teaser-card" markdown>
### [:material-shield-check: A 股规则补丁层](a-share-rules.md)

**8 个独立 pure-function 模块** + `AShareRuleChecklist` 自检。AKQuant 默认只 enforce `tick_size`，其余全部自研。

| 规则 | 模块 |
| --- | --- |
| 涨跌停 | `price_limits` |
| 停牌 | `suspension` (从 OHLCV 推断) |
| 除权除息 (qfq) | `ex_dividend` |
| ST 股票过滤 | `st_filter` |
| 100 股整手 | `lot_enforcement` |
| 印花税卖单边 | `stamp_tax` |
| 幸存者偏差 | `delisted_universe` |
| T+1 交割 | (AKQuant `t_plus_one=True`) |

- :源: [`backtest/a_share/`](https://github.com/shaok1814-lang/quant-platform/tree/master/backtest/a_share)
- :测试: 100+ (每规则 6+ 边界)

</div>

<div class="teaser-card" markdown>
### [:material-test-tube: 绩效与防过拟合](anti-overfit.md)

**Walk-Forward + Optuna + 参数敏感度** — CLAUDE.md 硬约束在 `research/factor_lib/analytics/` 里 enforce。

- `run_walk_forward(train_months=24, test_months=12, step_months=3)` — 拒绝 `step_months < test_months` 防止窗口重叠
- `WalkForwardResult.is_to_oos_decay` — 每个 metric 的 IS/OOS 衰减比（高优 ≥0.70 / 低优 ≤1.30）
- `param_sensitivity.assert_stable` — ±20% 网格扫描 + ±30%  容忍
- `optuna_runner.optimize_params` — 仅 Optuna（明确不用 grid search）

- :源: [`research/factor_lib/analytics/`](https://github.com/shaok1814-lang/quant-platform/tree/master/research/factor_lib/analytics)

</div>

<div class="teaser-card" markdown>
### [:material-server: 执行层 + Dashboard](architecture.md#live-gateway)

**Paper + Live 双模式** — 同一 `BrokerAdapter` Protocol 抽象两个后端：

- `AkquantPaperAdapter` (W7.1 Phase 1 默认) — 同步 fill 模拟，dev 机能跑（无 xtquant）
- `XtQuantLiveAdapter` (W7.1 Phase 2) — 真 miniQMT / xtquant（lazy-import，Windows only）

3 个 risk guards（10% 仓位 / 20 单日 / 5%  drawdown kill switch）+ SQLite journal + 3-page Streamlit dashboard（universe status / equity curves / paper trade history）。

- :源: [`execution/`](https://github.com/shaok1814-lang/quant-platform/tree/master/execution) + [`ops/`](https://github.com/shaok1814-lang/quant-platform/tree/master/ops)
- :测试: 175+ (risk / journal / runner / brokers / dashboard / scheduler)

</div>

</div>

---

## 下一步

- 想看每个模块怎么连起来 → [系统架构](architecture.md)
- 想跑通 + 看 dashboard → [快速开始](quickstart.md)
- 想看测试套件真实跑的样子 → [项目时间线](timeline.md)
- 想看每个模块的公开 API → [API 参考](api-reference.md)