# System Architecture

quant-platform is a **6-layer layered architecture**. CLAUDE.md §架构决策 hard constraints have explicit enforcement points at each layer.

## Layered view {#layers}

| Layer | Implementation | Responsibility |
| --- | --- | --- |
| **Data ingestion** {#data-source} | akshare (primary) + adata / baostock (validator) | Quotes, fundamentals, realtime |
| **Data storage** {#data-storage} | DuckDB + Parquet (self-research layer) | Adjust-factor / suspension / ST flag |
| **Core engine** {#engine} | **AKQuant directly used; no source modifications** | Event loop / order management / base backtest |
| **A-share patches** {#a-share-patches} | Self-research modules (on top of AKQuant) | ST / delisted / convertible bonds / price-limit boundaries |
| **Factor library** {#factor-library} | Fully self-research | Compute / winsorize / neutralize / standardize |
| **Performance attribution** {#performance} | Self-research | Includes Walk-Forward anti-overfit |
| **Live gateway** {#live-gateway} | Self-research (xtquant / miniQMT) | Connected only at P3 stage |

| Layer | Implementation | Responsibility |
| --- | --- | --- |
| **Data ingestion** | akshare (primary) + adata / baostock (validator) | Quotes, fundamentals, realtime |
| **Data storage** | DuckDB + Parquet (self-research layer) | Adjust-factor / suspension / ST flag |
| **Core engine** | **AKQuant directly used; no source modifications** | Event loop / order management / base backtest |
| **A-share patches** | Self-research modules (on top of AKQuant) | ST / delisted / convertible bonds / price-limit boundaries |
| **Factor library** | Fully self-research | Compute / winsorize / neutralize / standardize |
| **Performance attribution** | Self-research | Includes Walk-Forward anti-overfit |
| **Live gateway** | Self-research (xtquant / miniQMT) | Connected only at P3 stage |

!!! note "CLAUDE.md hard constraint"
    > Forbidden to modify AKQuant source directly; extend via inheritance / wrapping / patch layer. If modification is required, discuss in Issue first and evaluate switching to rqalpha.

## Data flow

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

## Execution pipeline

Strategy → bridge → runner → risk / journal / broker tri-fork:

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

Full module inventory: [API reference](api-reference.md) + each module's README.

## Roadmap

```mermaid
timeline
    P1 Data : W1 AKQuant validation + MA-cross : W2 Data layer (akshare + DuckDB)
    P2 Factors / Strategies : W3 Factor lib + strategy integration : W4 A-share rule patches : W5 Walk-Forward + Optuna
    P3 Execution + Monitoring : W6 Auto ingest + Dashboard : W7.1 Execution layer + paper discipline
```

Full version: [CLAUDE.md §6-week roadmap](https://github.com/shaok1814-lang/quant-platform/blob/master/CLAUDE.md).

## Anti-overfit: Walk-Forward rolling windows

[Walk-Forward](anti-overfit.md) is enforced project-wide — 24 months train, 12 months test, 3-month step, **no overlapping windows**:

```mermaid
gantt
    title Walk-Forward windows (24m train / 12m test / 3m step)
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

Each fold runs its own backtest + Optuna; per-metric IS/OOS decay ratio is logged. Full constraints: [anti-overfit](anti-overfit.md).

## A-share rules: coverage matrix

Each rule fires on a different event / on_bar action. The full 8-rule patch layer (pure-function + ≥6 boundary tests each) is documented on [A-Share Rules](a-share-rules.md):

```mermaid
flowchart LR
    A[on_bar event] --> B{event type?}

    B -->|buy intent| C[price_limits<br/>check limit-up]
    B -->|sell intent| D[price_limits<br/>check limit-down]
    B -->|every bar| E[suspension<br/>check halt]
    B -->|every bar| F[ex_dividend<br/>check qfq]
    B -->|universe build| G[st_filter<br/>exclude ST]
    B -->|every fill| H[lot_enforcement<br/>100-share lot]
    B -->|every fill| I[stamp_tax<br/>sell-only 0.1%]
    B -->|every fill| J[T+1<br/>next-day-sellable]

    style A fill:#e1f5ff
    style B fill:#fff4e1
```

The matrix shows **which rule corresponds to which on_bar event**,** preventing omissions.

---

## Next steps

- 8 A-share rule details → [A-Share Rules](../a-share-rules.md)
- 4 anti-overfit rules + Walk-Forward → [Anti-Overfit](../anti-overfit.md)
- 4-week paper discipline → [Paper-Validation Runbook](../paper-runbook.md)
- 17-framework survey → [Framework Survey](../framework-survey.md)