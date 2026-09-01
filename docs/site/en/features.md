# Features

quant-platform has tested, boundary-covered, immediately-runnable implementations at every layer. The table below maps to the source directories — click through for module-level docs.

<div class="teaser-grid" markdown>

<div class="teaser-card" markdown>
### [:material-database: Data layer](architecture.md#data-source)

**akshare (primary) + baostock (validator)** → Parquet (raw/clean) → DuckDB (`daily_bars` table, `(symbol, date)` PK for idempotent upsert). Daily 18:00 cron does incremental ingest, gated by 6 HARD quality checks before write.

- :Source: [`data_layer/`](https://github.com/shaok1814-lang/quant-platform/tree/master/data_layer)
- :Tests: 30+ (data_layer / ingestion / parquet / DuckDB / quality)
- :Deps: akshare · baostock · adata · tushare · duckdb · pyarrow

</div>

<div class="teaser-card" markdown>
### [:material-function-variant: Factor library](architecture.md#factor-library)

**4 alpha families + 3 cross-section post-processors + FactorPipeline composer**. pandas-only (deliberately not AKQuant polars DSL — keeps debuggability).

| Family | Factor | |
| | --- | --- |
| Trend | `ma_deviation(close, n=20)` | `(close - SMA) / SMA`, scale-invariant |
| Momentum | `n_day_return(close, n=20)` | pure N-day simple return |
| Mean-reversion | `rsi(close, n=14)`, `bollinger_z(close, n=20)` | Wilder RSI + Bollinger z-score |
| Liquidity | `turnover_ratio(volume, outstanding)` | volume / total outstanding |

Post-processors: `winsorize` (3σ / MAD / quantile) → `standardize` → `Neutralizer` (industry-neutralization hook).

- :Source: [`research/factor_lib/`](https://github.com/shaok1814-lang/quant-platform/tree/master/research/factor_lib)
- :Tests: 40+ (4 families × boundaries + post-processors + pipeline)

</div>

<div class="teaser-card" markdown>
### [:material-strategy: Strategies](architecture.md#engine)

**5 AKQuant Strategy subclasses** covering single-symbol and multi-symbol modes:

| Strategy | Category | Description |
| --- | --- | --- |
| `MACrossStrategy` | single, trend-following | 5/20 SMA cross, W1 baseline |
| `MACrossStrategy` (DuckDB driver) | single | same but with DuckDB data path |
| `TopNMeanReversionStrategy` | multi, cross-section | weekly rebalance, RSI + Bollinger z scoring |
| `FactorTimingMACross` | single, factor-as-filter | 5/20 cross + momentum factor gate (anti-spurious) |
| `DonchianBreakoutStrategy` | single, breakout | Turtle S1 simplified, 20-day-high entry / 10-day-low exit |

- :Source: [`research/strategies/`](https://github.com/shaok1814-lang/quant-platform/tree/master/research/strategies)
- :Tests: 9 + 10 + 14 + 9 (4 strategy e2es + bridge)

</div>

<div class="teaser-card" markdown>
### [:material-shield-check: A-share rules](a-share-rules.md)

**8 pure-function modules** + `AShareRuleChecklist` self-attestation. AKQuant only enforces `tick_size` by default; everything else is self-research.

| Rule | Module |
| --- | --- |
| Price limits | `price_limits` |
| Suspension | `suspension` (inferred from OHLCV) |
| Ex-dividend (qfq) | `ex_dividend` |
| ST stock filter | `st_filter` |
| 100-share lot | `lot_enforcement` |
| Stamp tax (sell only) | `stamp_tax` |
| Survivorship bias | `delisted_universe` |
| T+1 settlement | (AKQuant `t_plus_one=True`) |

- :Source: [`backtest/a_share/`](https://github.com/shaok1814-lang/quant-platform/tree/master/backtest/a_share)
- :Tests: 100+ (6+ boundaries per rule)

</div>

<div class="teaser-card" markdown>
### [:material-test-tube: Performance & anti-overfit](anti-overfit.md)

**Walk-Forward + Optuna + parameter sensitivity** — CLAUDE.md hard constraints enforced in `research/factor_lib/analytics/`.

- `run_walk_forward(train_months=24, test_months=12, step_months=3)` — rejects `step_months < test_months` (no overlapping windows)
- `WalkForwardResult.is_to_oos_decay` — per-metric IS/OOS decay ratio (higher-is-better ≥0.70 / lower-is-better ≤1.30)
- `param_sensitivity.assert_stable` — ±20% grid sweep + ±30% tolerance
- `optuna_runner.optimize_params` — Optuna-only (CLAUDE.md explicitly bans grid search)

- :Source: [`research/factor_lib/analytics/`](https://github.com/shaok1814-lang/quant-platform/tree/master/research/factor_lib/analytics)

</div>

<div class="teaser-card" markdown>
### [:material-server: Execution + Dashboard](architecture.md#live-gateway)

**Paper + Live dual-mode** — single `BrokerAdapter` Protocol abstracts both backends:

- `AkquantPaperAdapter` (W7.1 Phase 1 default) — synchronous fill simulation, runs on dev machines (no xtquant)
- `XtQuantLiveAdapter` (W7.1 Phase 2) — real miniQMT / xtquant (lazy-imported, Windows only)

3 risk guards (10% position cap / 20 daily trades / 5% drawdown kill switch) + SQLite journal + 3-page Streamlit dashboard (universe status / equity curves / paper trade history).

- :Source: [`execution/`](https://github.com/shaok1814-lang/quant-platform/tree/master/execution) + [`ops/`](https://github.com/shaok1814-lang/quant-platform/tree/master/ops)
- :Tests: 175+ (risk / journal / runner / brokers / dashboard / scheduler)

</div>

</div>

---

## Next steps

- See how each module connects → [Architecture](../architecture.md)
- Install + run + see the dashboard → [Quick Start](../quickstart.md)
- See how the test suite grew → [Project Timeline](../timeline.md)
- Public API for each module → [API Reference](../api-reference.md)