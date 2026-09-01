# Project Timeline

6 weeks from zero to P3 fully closed. The table below summarizes each week's deliverables + test count accumulation.

| Phase | Week | Focus | Key modules | Cumulative tests |
| | --- | --- | --- | --- |
| **P1 W1** | 1 | AKQuant validation + MA-cross smoke | `research/strategies/ma_cross.py` | ~48 |
| **P1 W2** | 2 | Data layer (akshare + Parquet + DuckDB + cross-source) | `data_layer/`, `ops/cross_source_job.py` | ~100 |
| **P2 W3** | 3 | Factor library + strategy integration | `research/factor_lib/` (4 families), `research/strategies/` (2 strategies) | ~148 |
| **P2 W4** | 4 | A-share rule patches (8 rules full coverage) | `backtest/a_share/` | ~250 |
| **P2 W5** | 5 | Walk-Forward + Optuna + parameter sensitivity | `research/factor_lib/analytics/` | ~330 |
| **P3 W6** | 6 | Auto ingest + Dashboard + cross-source audit + DingTalk alerts | `ops/ingest_job.py`, `ops/dashboard.py`, `ops/notify.py`, `ops/scheduler.py` | ~418 |
| **P3 W7.1** | 7 | Execution layer (paper + xtquant live + bridge + paper discipline) | `execution/` (protocol / risk / runner / brokers / journal / bridge), `ops/weekly_paper_job.py`, `scripts/run_paper_validation.py` | **537** |

!!! abstract "How to read this table"
- **Cumulative tests** doesn't mean "5 new tests = +5". It includes bug fixes, refactors, new modules, and additional boundary cases for old modules. `pytest -q` runs the full suite in ~30 seconds.
- **Key modules** column only lists code paths added or upgraded that week, not all work done that week.
- Full commit history + per-commit design decisions + bug post-mortems live in the [GitHub commit log](https://github.com/shaok1814-lang/quant-platform/commits/master).

## Why this is a real project (vs a tutorial demo)

!!! tip "A few details behind the 537 number"
- **48 → 537 is a real 6-week arc**, not written in one shot. There were ~5 real bug post-mortems along the way (W2.1 akshare proxy issue, W3 IntParam naming trap, W4 A-share rule coverage gap with AKQuant's actual implementation, W6.3 akshare vs baostock qfq drift of 67-363 bps, W7.1 cost-basis paper mode `max_drawdown_pct = 0%` ambiguity).
- **4 of the 8 hard constraints in CLAUDE.md have dedicated enforcement code** (10% position cap → `execution/risk.py:check_position_cap`, 5% drawdown kill → `execution/risk.py:check_drawdown_kill_switch`, 4-week paper discipline → `ops/weekly_paper_job.py` + `ops/scheduler.py`, anti-overfit → `research/factor_lib/analytics/`).
- **No modifications to AKQuant source** (CLAUDE.md core architecture decision) — all extensions via subclassing / wrapping / patch layer. `execution/brokers/akquant_paper.py` wrapping AKQuant's `MiniQMTTraderGateway` stub is the most explicit example.

## Next steps

- **4-week paper discipline starts now** — `python scripts/run_paper_validation.py` pre-flight → `python -m ops` launch. Sunday 9:00 CST auto-runs the paper session; first paper-vs-live deviation assessment after 4 weeks.
- **CLAUDE.md "single-strategy initial live capital ≤ 10% of total"** — once paper-vs-live deviation is < 5%, start live with first trade capital ≤ 10% of total.
- **More strategies / factors** — currently 5 strategies × 4 factor families. Next: Donchian System 2 (pyramiding + ATR sizing), Dual Momentum, pairs trading.