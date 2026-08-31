# Execution Layer

Personal A-share quant execution layer. Implements CLAUDE.md's
「合规与实盘纪律」: 4-week paper validation, 10% capital cap,
<5% paper-vs-live deviation.

## Layer diagram

```
strategy (AKQuant subclass) ─┐
                             │
                             ▼
              ┌──────────────────────────┐
              │  AkquantStrategyCallable │  ← execution.bridge
              │  (callable strategy)     │
              └──────────────────────────┘
                             │
                             ▼
              ┌──────────────────────────┐
              │  run_paper_session       │
              │  (bar → strategy → risk   │
              │   → adapter → journal)   │
              └──────────────────────────┘
                       │       │       │
                       ▼       ▼       ▼
                   ┌──────┐┌──────┐┌──────────┐
                   │ risk ││journal││ broker  │
                   └──────┘└──────┘└──────────┘
                                   │
                            ┌──────┴──────┐
                            ▼             ▼
                  AkquantPaperAdapter  XtQuantLiveAdapter
                  (W7.1: in-memory)   (W7.1 Phase 2: xtquant)
```

## Paper mode (no broker needed)

```python
from datetime import datetime
import pandas as pd
from pathlib import Path

from execution import (
    run_paper_session, OrderIntent, PaperJournal,
    AkquantPaperAdapter, RiskConfig,
)

today = datetime.now()
bars = pd.DataFrame({
    "date": [today + pd.Timedelta(minutes=i) for i in range(5)],
    "open": [10.0]*5, "high": [10.5]*5, "low": [9.5]*5,
    "close": [10.20, 10.30, 10.40, 10.50, 10.60],
    "volume": [1_000_000.0]*5,
})

state = {"bought": False}
def strategy(s, recent):
    if s.get("bought"): return []
    s["bought"] = True
    return [OrderIntent(
        client_order_id="smoke-1", symbol="000001",
        side="buy", quantity=100, price=10.20,
    )]

report = run_paper_session(
    strategy=strategy, data=bars,
    adapter=AkquantPaperAdapter(),
    journal=PaperJournal(Path("data/journal/paper.sqlite")),
)
print(f"intents={report.n_intents} filled={report.n_filled} equity={report.final_equity:.2f}")
```

Works on any machine, no Windows / no xtquant / no broker.

## Paper mode with W3 strategy bridge

Wrap an existing AKQuant strategy (e.g. `MACrossStrategy` from
W3) so it satisfies the runner's callable contract:

```python
from execution.bridge import AkquantStrategyCallable
from execution import run_paper_session, AkquantPaperAdapter, PaperJournal
from research.strategies.ma_cross import MACrossStrategy
from pathlib import Path

bridge = AkquantStrategyCallable(MACrossStrategy, symbol="000001")
report = run_paper_session(
    strategy=bridge, data=bars,
    adapter=AkquantPaperAdapter(),
    journal=PaperJournal(Path("data/journal/ma_cross.sqlite")),
    # After each fill, sync the bridge's fake-position mirror:
)
# Phase 3 will wire run_paper_session to call bridge.update_position
# after each successful fill. For now, do it manually if you want
# the strategy to see accurate position.size on subsequent bars.
```

The bridge:
- Overrides `order_target_percent` to capture `OrderIntent` instead
  of submitting through AKQuant's normal execution backend.
- Patches `get_history_df(count)` to return a slice of the runner's
  `recent_bars`.
- Maintains a `FakePosition` mirror (`bridge.update_position(...)`)
  so the strategy sees accurate `self.position.size`.

## Live mode (Windows + xtquant + miniQMT)

Prerequisites:
- Windows 10/11
- miniQMT client (`XtMiniQmt.exe`) installed and logged in once
- Python 3.10 or 3.11 (xtquant doesn't support 3.13)
- `pip install xtquant` (the SDK ships with miniQMT — copy
  `bin.x64/Lib/site-packages/xtquant` into your Python
  site-packages if pip can't find it)
- A miniQMT account with `userdata_mini` folder created

```python
import time
from pathlib import Path

from execution import (
    run_paper_session, PaperJournal, OrderIntent,
)
from execution.brokers.xtquant_live import XtQuantLiveAdapter
from execution.bridge import AkquantStrategyCallable
from research.strategies.ma_cross import MACrossStrategy

adapter = XtQuantLiveAdapter(
    path="D:/国金QMT/userdata_mini",   # or your broker's path
    session_id=int(time.time() * 1000),  # unique per session
    account_id="YOUR_ACCOUNT_ID",
)
adapter.connect()

bridge = AkquantStrategyCallable(MACrossStrategy, symbol="000001")

journal = PaperJournal(Path("data/journal/live.sqlite"))
report = run_paper_session(
    strategy=bridge, data=bars,
    adapter=adapter, journal=journal,
)
print(f"live: intents={report.n_intents} filled={report.n_filled}")

adapter.disconnect()
```

**CLAUDE.md 「≥4 周模拟盘」 hard constraint**: do NOT use
`XtQuantLiveAdapter` with real money until you've run 4 weeks
of paper trading with `AkquantPaperAdapter` on the SAME strategy
+ same parameters. Use `journal.compare_to(paper_journal,
max_deviation_pct=5.0)` to confirm paper-vs-live deviation < 5%.

## What the runner does (per bar)

1. Snapshot equity (`adapter.query_account()`); write to journal.
2. Check drawdown kill switch (lifetime HWM). If breached,
   activate session-wide stop.
3. Call `strategy(state, recent_bars)`. Capture returned
   `OrderIntent`s.
4. For each intent:
   - `check_position_cap` (10% of equity).
   - `check_daily_trade_count` (20 round-trips/day).
   - If both pass → `adapter.place_order(intent)`.
   - Record the intent + execution report + (if filled) a Fill row.
5. Risk rejection also writes a journal row (so you can audit
   "why didn't this order go through" later).

## Module reference

| Module | Purpose |
|---|---|
| `execution.protocol` | Frozen dataclasses: `OrderIntent`, `ExecutionReport`, `Position`, `Fill`, `EquitySnapshot`, `RiskConfig`. |
| `execution.risk` | `Allow` / `Reject` sum type + 3 pure-function checks. |
| `execution.journal` | SQLite-backed `PaperJournal` with `compare_to` for paper-vs-live deviation. |
| `execution.runner` | `run_paper_session` entry point. |
| `execution.brokers.base` | `BrokerAdapter` Protocol. |
| `execution.brokers.akquant_paper` | Paper backend (wraps AKQuant MiniQMTTraderGateway stub). |
| `execution.brokers.xtquant_live` | Live backend (Phase 2). xtquant on Windows. |
| `execution.brokers.xtquant_callbacks` | `XtQuantTradeCallback` — queue bridge. |
| `execution.brokers.xtquant_fake` | `FakeXtQuantTrader` — test driver. |
| `execution.brokers.xtquant_models` | Slim shapes (XtOrder, XtTrade, ...). |
| `execution.brokers.registry` | `BrokerRegistry` — name → factory. |
| `execution.bridge.akquant_strategy` | `AkquantStrategyCallable` — AKQuant Strategy → runner callable. |

## Testing

```bash
uv run pytest tests/test_execution_xtquant_fake.py \
                tests/test_execution_xtquant_callbacks.py \
                tests/test_execution_xtquant_adapter.py \
                tests/test_execution_bridge_akquant.py \
                tests/test_execution_protocol.py \
                tests/test_execution_risk.py \
                tests/test_execution_journal.py \
                tests/test_execution_akquant_paper.py \
                tests/test_execution_runner.py -v
```

All tests run on any platform. xtquant is NOT installed locally;
the adapter tests use `FakeXtQuantTrader` as the trader. Windows
+ xtquant + miniQMT is required only to actually run a live
session.

## Phase 3+ deferred

- 钉聊 SOFT alert on kill switch (today: loguru WARNING only).
- W6.2 dashboard trade history page (uses journal query API).
- W6.5 weekly cron (`0 9 * * 0` Sunday 9:00 CST, separate from
  W6.1's 18:00 daily ingest).
- Multi-symbol bridge (Phase 3 M2/M3).
- Multi-account support.
- xtquant `xtdata` real-time market data subscription (Phase 4).
