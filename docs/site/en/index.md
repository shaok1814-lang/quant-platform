# Personal A-Share Quant Research & Trading System

> A self-research A-share quant platform built on AKQuant — data complete, rules complete, anti-overfit discipline complete, paper-trading validated.

[Features :material-arrow-right:](features.md){ .md-button .md-button--primary }
[Quick Start :material-rocket-launch:](../quickstart.md){ .md-button }
[GitHub :fontawesome-brands-github:](https://github.com/shaok1814-lang/quant-platform){ .md-button }

---

## What is this?

quant-platform is a **personal A-share quantitative research & execution system**. We picked [AKQuant](https://github.com/) as the backtest / matching engine skeleton (the core architecture decision in CLAUDE.md) and layered self-research modules for the A-share rule patches, factor library, execution layer, and ops monitoring on top.

The goal is not "yet another quant framework" but to make the hard constraints in [CLAUDE.md](https://github.com/shaok1814-lang/quant-platform/blob/master/CLAUDE.md) —

> Single-strategy initial live capital ≤ 10% of total; ≥4 weeks of paper trading before live; every live-trading code path must be validated with < 5% paper-vs-live deviation.

— actually **land** in a 537-test codebase.

## By the numbers

<div class="stat-grid" markdown>
<div class="stat-tile" markdown>
<span class="value">5</span>
<span class="label">AKQuant strategies</span>
</div>
<div class="stat-tile" markdown>
<span class="value">8</span>
<span class="label">A-share rule patches</span>
</div>
<div class="stat-tile" markdown>
<span class="value">537</span>
<span class="label">passing unit tests</span>
</div>
<div class="stat-tile" markdown>
<span class="value">7</span>
<span class="label">weeks 0 → P3</span>
</div>
</div>

<div class="teaser-grid" markdown>

<div class="teaser-card" markdown>
### [:material-shield-check: A-share rules complete](a-share-rules.md)

Price-limits / suspension / ex-div / ST / 100-share lot / stamp tax / survivorship — 8 pure-function modules, each with ≥6 boundary tests.

</div>

<div class="teaser-card" markdown>
### [:material-test-tube: Anti-overfit discipline](anti-overfit.md)

Walk-Forward 24m/12m/3m + Optuna + ±20% param sensitivity + IS/OOS decay < 30% — CLAUDE.md hard constraints enforced in code.

</div>

<div class="teaser-card" markdown>
### [:material-rocket-launch: Paper to live](paper-runbook.md)

Sunday 9:00 cron runs the MACrossStrategy paper session automatically + DingTalk alerts + dashboard drill-down. 4-week paper discipline automated.

</div>

</div>

## Tech stack

| Dimension | Choice |
| |  |
| --- | --- |
| Data sources | akshare (primary) · baostock (cross-source validator) · adata · tushare |
| Storage | DuckDB + Parquet, akshare `qfq` 前复权 as canonical adjust-mode |
| Engine | AKQuant (no source modifications — only subclass / wrap to extend) |
| Factors | pandas + numpy (deliberately not AKQuant polars DSL — keep debuggability) |
| Optimization | Optuna (CLAUDE.md explicitly bans grid search) |
| Scheduling | APScheduler (daily 18:00 ingest + Sunday 9:00 paper cron) |
| Alerts | DingTalk webhook (env-gated, default inactive) |
| Dashboard | Streamlit + Plotly |
| Testing | pytest (537 tests, ~30s full suite) |

Full list on the [Tech Stack page](../tech-stack.md).

---

## Next steps

- 30 seconds to install + run tests → [Quick Start](../quickstart.md)
- See the 6-layer architecture → [Architecture](../architecture.md)
- How the project went W1 → W7.1 → [Project Timeline](../timeline.md)
- 17-framework survey → [Framework Survey](../framework-survey.md)