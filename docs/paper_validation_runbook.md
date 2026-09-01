# Paper-Validation Runbook (W7.1 + W6.5)

This is the operator runbook for the **4-week paper-validation
cycle** that CLAUDE.md requires before any live trading:

> 实盘前必须经过至少 4 周模拟盘验证。所有实盘代码必须经过
> 模拟盘对比验证偏差 < 5%。

The infrastructure is in place (W6.5 cron + W7.1 runner +
dashboard). This runbook is the procedure to **start**, **monitor**,
and **graduate** from paper to live.

---

## 1. Pre-flight (one-time, before launch)

```bash
python scripts/run_paper_validation.py
```

Checks:

| Check | What it verifies |
|---|---|
| DuckDB exists | `data/duckdb/daily.duckdb` populated by daily ingest |
| DuckDB data | ≥30 rows for `000001` in the lookback window |
| 钉聊 env | `DINGTALK_WEBHOOK_URL` set (WARN if not — alerts degrade gracefully) |
| Output dir | `data/paper_reports/` writable |
| AKQuant | `import akquant` succeeds (Windows: install via uv) |
| Scheduler | `build_scheduler()` returns 2 jobs (daily_ingest + weekly_paper) |

Output ends with `PRE-FLIGHT PASSED.` or `PRE-FLIGHT FAILED (N blocker(s)).`.

---

## 2. Launch the scheduler (long-running)

The scheduler runs in the foreground as a blocking main loop. On
production Windows it should be launched via **Task Scheduler**
(start at boot, restart on exit); on Linux via systemd / launchd /
supervisord.

```bash
python -m ops
```

The scheduler registers two cron jobs:

| Job ID | Trigger | Function |
|---|---|---|
| `daily_ingest` | Every day 18:00 Asia/Shanghai | `ops.ingest_job.run_daily_ingest` |
| `weekly_paper` | Every Sunday 9:00 Asia/Shanghai | `ops.weekly_paper_job.run_weekly_paper_session` |

Override via env vars:

| Env var | Default |
|---|---|
| `OPS_INGEST_HOUR` / `OPS_INGEST_MINUTE` / `OPS_INGEST_TZ` | 18:00 Asia/Shanghai |
| `OPS_WEEKLY_PAPER_ENABLED` | `1` (set `0` to disable) |
| `OPS_WEEKLY_PAPER_DAY` | `sun` |
| `OPS_WEEKLY_PAPER_HOUR` / `OPS_WEEKLY_PAPER_MINUTE` | 9:00 |

To stop: `Ctrl+C` (Windows + APScheduler handles cleanly).

---

## 3. What the weekly paper job does

Every Sunday 9:00 Asia/Shanghai, before market open:

1. Load last **60 calendar days** of OHLCV for `000001` from DuckDB.
2. Wrap `MACrossStrategy` (W1 baseline) in `AkquantStrategyCallable`.
3. Run `run_paper_session` with `AkquantPaperAdapter` + a fresh
   per-rotation SQLite journal (`journal_<YYYY-MM-DD>.sqlite`).
4. Write a `WeeklyPaperReport` JSON to
   `data/paper_reports/weekly_<YYYY-MM-DD>.json`.
5. If the drawdown kill switch fired during the session, fire
   `ops.notify.ding(title, body)` with a stable body format.

After 4 weeks you have **4 JSON files + 4 SQLite journals**, one
per Sunday. The dashboard reads these and shows them on the
"Paper Trade History" page.

---

## 4. Monitoring

### 4.1 Dashboard (interactive)

```bash
streamlit run ops/dashboard.py
```

Three pages:

- **Universe Status** — per-symbol DuckDB row counts + gap detection
- **Equity Curves** — per-symbol AKQuant backtest NAVs (research)
- **Paper Trade History** — weekly runs + drill-down fills + intents

The Paper Trade History page is the primary operator console during
the 4-week cycle. KPI tiles show the latest run's `final_equity` /
`max_drawdown` / `kill_switch_fired`.

### 4.2 钉聊 alerts (push)

The W6.5 weekly cron fires 钉聊 when:

- The drawdown kill switch flips 0→1 during the weekly paper run.
  Title: `Weekly paper kill switch (000001)`. Body fields:
  `run_date=`, `symbol=`, `window=`, `final_equity=`,
  `max_drawdown_pct=`, `n_filled=`, `report=`.

The W7.1 Phase 5 XtQuantLiveAdapter (live mode only) fires:

- Reconnect exhausted — `XtQuant reconnect exhausted (<account>)`
- Drop count > N — `XtQuant event drop count > N (<account>)`

### 4.3 On-disk artifacts

```bash
ls data/paper_reports/
# weekly_2026-08-31.json
# weekly_2026-09-07.json
# weekly_2026-09-14.json
# weekly_2026-09-21.json
# journal_2026-08-31.sqlite
# ...
```

The SQLite journals hold the per-bar fill / intent / snapshot rows.
Use any SQLite browser (or the dashboard) to inspect. To inspect
rawly:

```python
import sqlite3

con = sqlite3.connect("data/paper_reports/journal_2026-09-21.sqlite")
print(con.execute("SELECT * FROM fill").fetchall())
```

---

## 5. Graduation criteria (paper → live)

CLAUDE.md hard constraints:

1. **≥ 4 weeks of consecutive paper runs** without a kill-switch
   fire. (CLAUDE.md 「实盘前必须经过至少 4 周模拟盘验证」)
2. **Paper-vs-live deviation < 5%** once live trading starts.
   Use `journal.compare_to(paper_journal, live_journal, max_deviation_pct=5.0)`.
3. **Single strategy, ≤ 10% of capital** for the first live run.
   (CLAUDE.md 「单策略初始实盘资金不超过总资金 10%」)

Kill-switch fire during the 4-week cycle:

- The cycle is NOT invalidated by one kill-switch fire — it's a
  warning that the strategy's drawdown profile is closer to the
  5% cap than expected. Investigate before graduating:
  - Look at the `WeeklyPaperReport.max_drawdown_pct` trend across
    the 4 weeks.
  - Read the journal for the week — which trade(s) triggered?
  - If the drawdown is structural (the strategy consistently
    loses), reduce position size or re-fit the strategy.

---

## 6. Smoke (one-time, optional)

Verify the full path BEFORE launching the scheduler:

```bash
python scripts/run_paper_validation.py --smoke --weeks 3
```

This runs the weekly paper job 3 times back-to-back, writing to
`data/paper_reports/weekly_<today>.json`. Each run overwrites the
previous (because the date is `today`); this is intended as a
warm-up, not a 4-week simulation. For real 4-week simulation,
launch the scheduler and let the cron run.

---

## 7. Quick reference

```bash
# Pre-flight (one-time)
python scripts/run_paper_validation.py

# Launch scheduler (long-running; the actual validation cycle)
python -m ops

# Smoke (one-time, optional; verifies full path before launch)
python scripts/run_paper_validation.py --smoke --weeks 3

# Dashboard (interactive monitor)
streamlit run ops/dashboard.py

# Inspect a journal manually
sqlite3 data/paper_reports/journal_<date>.sqlite "SELECT * FROM fill"
```

---

## 8. What this runbook does NOT cover

- **Live trading setup** — `XtQuantLiveAdapter` constructor args,
  Windows miniQMT client install, `xtquant` pip install. See
  `execution/README.md` § Live mode.
- **Strategy re-fit** — if the kill-switch fires, the runbook says
  "investigate"; the actual re-fit is a research workflow (see
  `research/strategies/` + `backtest/walk_forward.py`).
- **Multi-symbol validation** — the W6.5 default is single-symbol
  (`000001`). Multi-symbol validation is Phase 5+ (see
  [[w7-1-phase4-status]] for the bridge + multi-symbol paper
  session path).