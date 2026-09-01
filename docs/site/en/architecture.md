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