# API Reference

!!! abstract "Usage"
    This page is the project's module index + entry-point links. Each module's docstrings + signatures are viewable in the GitHub source (click a module name to jump). Full `mkdocstrings` auto-API will be enabled in Phase 3.

## Top-level module structure

```text
quant-platform/
├── data_layer/         # Data layer (akshare → Parquet → DuckDB)
├── research/
│   ├── factor_lib/     # Factor library (4 families + post + analytics)
│   └── strategies/     # 5 AKQuant Strategy classes
├── backtest/
│   └── a_share/        # 8 A-share rule pure-function modules
├── execution/          # Execution layer (risk / runner / brokers / journal)
└── ops/                # Monitoring (ingest / dashboard / scheduler / notify)
```

## Public API (by layer)

### Data layer

| Module | Entry |
| --- | --- |
| `data_layer.ingestion.akshare_fetcher` | [`fetch_daily_bars(symbol, start, end, adjust="qfq")`](https://github.com/shaok1814-lang/quant-platform/blob/master/data_layer/ingestion/akshare_fetcher.py) |
| `data_layer.ingestion.akshare_fetcher` | [`fetch_daily_bars_with_fallback(...)`](https://github.com/shaok1814-lang/quant-platform/blob/master/data_layer/ingestion/akshare_fetcher.py) — akshare failure → automatic baostock fallback |
| `data_layer.storage.duck` | [`DuckStore(path)`](https://github.com/shaok1814-lang/quant-platform/blob/master/data_layer/storage/duck.py) — `.upsert_daily_bars(df)` / `.query_daily_bars(symbol, ...)` |
| `data_layer.storage.parquet_io` | `write_raw()` / `read_raw()` + `df.attrs` metadata round-trip |
| `data_layer.validation.cross_source` | `validate(df_a, df_b, threshold_bps=50.0)` — akshare ↔ baostock reconciliation |

### Factor library

| Module | Entry |
| --- | --- |
| `research.factor_lib.trend` | `ma_deviation(close, bar_window=20)` |
| `research.factor_lib.momentum` | `n_day_return(close, window=20)` |
| `research.factor_lib.mean_reversion` | `rsi(close, window=14)`, `bollinger_z(close, window=20)` |
| `research.factor_lib.liquidity` | `turnover_ratio(volume, outstanding_share)` |
| `research.factor_lib.post` | `winsorize(series, method="3sigma")`, `standardize(series)`, `Neutralizer` |
| `research.factor_lib.pipeline` | `FactorPipeline(factors=[...], winsorize=..., standardize=..., neutralizer=...)` |
| `research.factor_lib.analytics.performance` | `summarize_metrics(result, phase="is"\|"oos")`, `oos_decay()` |
| `research.factor_lib.analytics.walk_forward` | `run_walk_forward(train_months=24, test_months=12, step_months=3)` |
| `research.factor_lib.analytics.param_sensitivity` | `param_sensitivity_scan(...)`, `assert_stable(...)` |
| `research.factor_lib.analytics.optuna_runner` | `optimize_params(study=..., data=...)` |

### Strategies

| Class | Source |
| --- | --- |
| `MACrossStrategy` (fast_window / slow_window) | [`research/strategies/ma_cross.py`](https://github.com/shaok1814-lang/quant-platform/blob/master/research/strategies/ma_cross.py) |
| `TopNMeanReversionStrategy` (top_n / rsi_window / boll_window / rebalance_weekday) | [`research/strategies/topn_mean_reversion.py`](https://github.com/shaok1814-lang/quant-platform/blob/master/research/strategies/topn_mean_reversion.py) |
| `FactorTimingMACross` (fast_window / slow_window / factor_window) | [`research/strategies/factor_timing.py`](https://github.com/shaok1814-lang/quant-platform/blob/master/research/strategies/factor_timing.py) |
| `DonchianBreakoutStrategy` (entry_window / exit_window) | [`research/strategies/donchian_breakout.py`](https://github.com/shaok1814-lang/quant-platform/blob/master/research/strategies/donchian_breakout.py) |

Each strategy has a `run_demo()` function (duckdb_path / akshare fallback).

### A-share rules

8 standalone modules, each pure-function:

| Module | Public API |
| --- | --- |
| `backtest.a_share.price_limits` | `compute_limit_price(prev_close, board, is_st)`, `is_at_limit(...)`, `is_limit_up(...)`, `is_limit_down(...)` |
| `backtest.a_share.suspension` | `infer_suspension_from_ohlcv(bars)` |
| `backtest.a_share.ex_dividend` | `detect_ex_dividend_days(bars)` |
| `backtest.a_share.st_filter` | `filter_st(symbols, include_st=False)`, `fetch_st_symbols(allow_network=False)` |
| `backtest.a_share.lot_enforcement` | `enforce_lot(qty, lot_size=100)`, `is_valid_lot(qty)` |
| `backtest.a_share.stamp_tax` | `compute_stamp_tax(notional, side='buy'\|'sell')` |
| `backtest.a_share.delisted_universe` | `build_universe(include_delisted=True)`, `fetch_delisted_symbols(allow_network=False)` |
| `backtest.a_share` | `AShareRuleChecklist` (NamedTuple self-attestation) |

### Execution layer

| Module | Public API |
| --- | --- |
| `execution.protocol` | `OrderIntent`, `ExecutionReport`, `ExecutionStatus`, `Position`, `Fill`, `EquitySnapshot`, `RiskConfig`, `OrderType`, `Side` (frozen dataclasses) |
| `execution.risk` | `Allow`, `Reject`, `RiskDecision`, `check_position_cap`, `check_daily_trade_count`, `check_drawdown_kill_switch` |
| `execution.runner` | `run_paper_session(strategy, data, adapter, journal, risk_cfg, session_cfg)`, `run_multi_account_paper_session(accounts, data, session_cfg)`, `PaperSessionConfig`, `PaperSessionReport`, `AccountSlot`, `MultiAccountReport` |
| `execution.journal` | `PaperJournal(path)` — `.record_intent()`, `.record_report()`, `.record_fill()`, `.query_fills(day)`, `.query_intents(day)`, `.compute_daily_trade_count(day)`, `.compare_to(other, max_deviation_pct=...)` |
| `execution.brokers.akquant_paper` | `AkquantPaperAdapter()` — dev / CI default |
| `execution.brokers.xtquant_live` | `XtQuantLiveAdapter(path, session_id, account_id, ...)` — Windows-only, lazy-imports xtquant |
| `execution.brokers.registry` | `register_broker()`, `create_registered_broker()`, `list_registered_brokers()` |
| `execution.bridge.akquant_strategy` | `AkquantStrategyCallable(strategy_cls, symbol=...)` — wrap AKQuant Strategy as a runner-callable |

### Ops

| Module | Public API |
| --- | --- |
| `ops.universe` | `load_universe(path=None)`, `UniverseEntry` |
| `ops.quality` | `check_quality(df, symbol)` — 6 HARD/SOFT checks |
| `ops.notify` | `ding(title, body, at_mobiles=None)` — DingTalk webhook, env-gated |
| `ops.ingest_job` | `run_daily_ingest(date=None, ...)`, `ingest_window(start_date, end_date, ...)` |
| `ops.weekly_paper_job` | `run_weekly_paper_session(...)` |
| `ops.cross_source_job` | `run_cross_source_audit(...)` |
| `ops.scheduler` | `build_scheduler(hour, minute, tz, enable_weekly_paper, ...)` |
| `ops.dashboard_data` | `load_universe_status()`, `load_symbol_bars()`, `compute_strategy_equity()`, `load_paper_run_summaries()`, `load_paper_fills()`, `load_paper_intents()` |
| `ops.dashboard` | Streamlit entry — `streamlit run ops/dashboard.py` |

### Scripts

| Script | Entry |
| --- | --- |
| `scripts/run_paper_validation.py` | 6 pre-flight checks + `--smoke --weeks N` |

## Phase 3 plan

Full `mkdocstrings` auto-API will be enabled in Phase 3, rendering signatures + docstrings for each module. This page will then become fully automated.

Phase 1 uses the entry-point table + GitHub source links — keeps `mkdocs build --strict` clean while still giving the operator a complete index.