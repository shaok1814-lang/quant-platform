# 系统架构

quant-platform 是 **6 层分层架构**，CLAUDE.md §架构决策 的硬约束在每一层都有具体执行点。

## 分层视图 {#layers}

| 层 | 实现 | 职责 |
| --- | --- | --- |
| **数据采集** {#data-source} | akshare (主) + adata / baostock (校验) | 行情、财务、实时 |
| **数据存储** {#data-storage} | DuckDB + Parquet (自研层) | 复权 / 停牌 / ST 标记 |
| **核心引擎** {#engine} | **AKQuant 直接使用，不修改其源码** | 事件循环 / 订单管理 / 基础回测 |
| **A 股补丁** {#a-share-patches} | 自研模块（在 AKQuant 之上） | ST / 退市 / 可转债 / 涨跌停边界 |
| **因子库** {#factor-library} | 完全自研 | 计算 / 去极值 / 中性化 / 标准化 |
| **绩效归因** {#performance} | 自研 | 含 Walk-Forward 防过拟合 |
| **实盘网关** {#live-gateway} | 自研（xtquant / miniQMT） | 仅在 P3 阶段接入 |

| 层 | 实现 | 职责 |
| --- | --- | --- |
| **数据采集** | akshare (主) + adata / baostock (校验) | 行情、财务、实时 |
| **数据存储** | DuckDB + Parquet (自研层) | 复权 / 停牌 / ST 标记 |
| **核心引擎** | **AKQuant 直接使用，不修改其源码** | 事件循环 / 订单管理 / 基础回测 |
| **A 股补丁** | 自研模块（在 AKQuant 之上） | ST / 退市 / 可转债 / 涨跌停边界 |
| **因子库** | 完全自研 | 计算 / 去极值 / 中性化 / 标准化 |
| **绩效归因** | 自研 | 含 Walk-Forward 防过拟合 |
| **实盘网关** | 自研（xtquant / miniQMT） | 仅在 P3 阶段接入 |

!!! note "CLAUDE.md 硬约束"
    > 禁止直接修改 AKQuant 源码；通过继承 / 包装 / 补丁层实现扩展。如果必须修改，先在 Issue 中讨论并评估是否切换到 rqalpha。

## 数据流

```mermaid
flowchart LR
    A[akshare daily fetch] --> B[parquet<br/>data/raw, data/clean]
    B --> C[DuckDB<br/>daily_bars table]
    C --> D[research/factor_lib<br/>pandas + numpy]
    D --> E[research/strategies<br/>5 AKQuant strategies]
    E --> F[AKQuant run_backtest]
    F --> G[research/factor_lib/analytics<br/>walk-forward + optuna]
    G --> H[execution/runner<br/>paper session]
    H --> I[AkquantPaperAdapter]
    H -.live mode.-> J[XtQuantLiveAdapter]
    C --> K[ops/dashboard<br/>Streamlit]
    K --> L[Operator UI<br/>localhost:8501]
```

## 执行层 pipeline

策略 → bridge → runner → risk / journal / broker 三条线分流：

```mermaid
flowchart TD
    S[strategy<br/>AKQuant Strategy subclass] --> B[AkquantStrategyCallable<br/>bridge]
    B --> R[run_paper_session]
    R --> R1[snapshot equity<br/>+kill switch check]
    R1 --> R2[strategy emit<br/>OrderIntent]
    R2 --> R3[risk check<br/>position_cap / daily_count / kill]
    R3 --> R4[adapter.place_order]
    R4 --> R5[journal.record_fill<br/>SQLite]
    R4 -.live.-> R6[XtQuantTradeCallback<br/>queue.Queue cross-thread]
    R6 -.-> R5

    classDef riskStyle fill:#ffe6e6
    classDef journalStyle fill:#e6f0ff
    classDef brokerStyle fill:#e6ffe6
    class R3 riskStyle
    class R5 journalStyle
    class R4,R6 brokerStyle
```

## 路线图

```mermaid
timeline
    P1 数据 : W1 AKQuant 验证 + MA-cross : W2 数据层 (akshare + DuckDB)
    P2 因子 / 策略 : W3 因子库 + 策略集成 : W4 A 股规则补丁 : W5 Walk-Forward + Optuna
    P3 执行 + 监控 : W6 自动 ingest + Dashboard : W7.1 执行层 + 模拟盘纪律
```

完整版本：[CLAUDE.md §6 周路线图](https://github.com/shaok1814-lang/quant-platform/blob/master/CLAUDE.md)。

## 防过拟合：Walk-Forward 滚动窗口 {#walk-forward-windows}

[Walk-Forward](anti-overfit.md) 是本项目强制执行的的验证流程 — 训练 24 个月、测试 12 个月、季度滚动 3 个月，**不允许重叠**：

```mermaid
gantt
    title Walk-Forward 验证窗口（24m train / 12m test / 3m step）
    dateFormat YYYY-MM
    axisFormat %Y-%m

    section Fold 1
    Train (24m)    :a1, 2023-01, 24M
    Test (12m)     :a2, after a1, 12M

    section Fold 2
    Train (24m)    :b1, 2023-04, 24M
    Test (12m)     :b2, after b1, 12M

    section Fold 3
    Train (24m)    :c1, 2023-07, 24M
    Test (12m)     :c2, after c1, 12M
```

每个 fold 独立 backtest，独立 Optuna，统计每个 metric 的 IS/OOS 衰减比。完整约束见 [防过拟合](anti-overfit.md)。

## A 股规则：覆盖矩阵 {#rule-coverage-matrix}

每条规则对应不同事件触发 / 不同 bar 处理动作。完整 patch 层 8 规则、纯函数 + ≥6 边界单元测试的详情见 [A 股规则](a-share-rules.md)：

```mermaid
flowchart LR
    A[on_bar event] --> B{event type?}

    B -->|buy intent| C[price_limits<br/>检查涨停]
    B -->|sell intent| D[price_limits<br/>检查跌停]
    B -->|every bar| E[suspension<br/>检查停牌]
    B -->|every bar| F[ex_dividend<br/>检查除权]
    B -->|universe build| G[st_filter<br/>过滤 ST]
    B -->|every fill| H[lot_enforcement<br/>100 股整手]
    B -->|every fill| I[stamp_tax<br/>卖单边 0.1%]
    B -->|every fill| J[T+1<br/>次日才可卖]

    style A fill:#e1f5ff
    style B fill:#fff4e1
```

矩阵决定了**哪条规则对应哪种 on_bar event**，避免遗漏。

---

## 下一步

- 8 条 A 股规则的细节 → [A 股规则](a-share-rules.md)
- 防过拟合 4 条规则 + Walk-Forward → [防过拟合](anti-overfit.md)
- 4 周模拟盘纪律 → [模拟盘手册](paper-runbook.md)
- 想看 17 框架对比 → [框架调研](framework-survey.md)