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
)
# The runner auto-calls ``bridge.update_position(symbol, qty, avg)``
# after each successful fill (Phase 4). The strategy sees accurate
# ``self.position.size`` on the next bar without manual wiring.
```

The bridge:
- Overrides `order_target_percent` to capture `OrderIntent` instead
  of submitting through AKQuant's normal execution backend.
- Patches `get_history_df(count)` to return a slice of the runner's
  `recent_bars`.
- Maintains a `FakePosition` mirror (synced by the runner via
  `bridge.update_position(...)` after each fill) so the strategy
  sees accurate `self.position.size`.
- Supports both **single-symbol** (`symbol="000001"`) and
  **multi-symbol** (default — pass `data={symbol: df, ...}` to
  `run_paper_session`; strategy emits intents with explicit
  `symbol=` args).

## Multi-symbol paper mode (W7.1 Phase 4)

For strategies that trade a basket (e.g. W3's
`TopNMeanReversionStrategy`), pass a `dict[str, pd.DataFrame]` to
the runner and use the bridge with no `symbol=` constructor arg:

```python
from execution.bridge import AkquantStrategyCallable
from execution import run_paper_session, AkquantPaperAdapter, PaperJournal
from research.strategies.topn_mean_reversion import TopNMeanReversionStrategy
from pathlib import Path

bridge = AkquantStrategyCallable(TopNMeanReversionStrategy)  # multi-symbol
bars_per_symbol = {
    "000001": df_000001,
    "600000": df_600000,
    "000002": df_000002,
}
report = run_paper_session(
    strategy=bridge, data=bars_per_symbol,
    adapter=AkquantPaperAdapter(),
    journal=PaperJournal(Path("data/journal/multi.sqlite")),
)
```

The runner drives `on_bar` once per symbol per bar (per-symbol
positional `active_symbol` is swapped on the bridge before each
call so `self.position.size` reads the right symbol's quantity).
Captured intents carry their own `symbol=`; the runner fans them
through risk → adapter → journal independently. Each symbol's
FakePosition is synced after its fills, so cross-symbol strategies
emit `order_target_percent(symbol=Y)` from inside `on_bar(X)` and
still see correct per-symbol position mirrors.

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
   activate session-wide stop AND fire `notify_fn` (see
   **Notifications** below).
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

## Phase 4 status

Closed (this commit):

- **Multi-symbol bridge** — `AkquantStrategyCallable(symbol=None)`
  routes `order_target_percent(symbol=X)` per-symbol; `get_history_df
  (count=, symbol=)` and `self.position` are scoped per-symbol.
- **Runner auto-sync of `bridge.update_position` after fill** — the
  runner reads `adapter.query_positions()` post-fill and forwards
  to `bridge.update_position` (skipped silently for plain callables;
  exceptions swallowed).

Still deferred (Phase 4+ future):

- W6.2 dashboard trade history page (uses journal query API).
- W6.5 weekly cron (`0 9 * * 0` Sunday 9:00 CST, separate from
  W6.1's 18:00 daily ingest).
- Multi-account support.
- xtquant `xtdata` real-time market data subscription.
- 钉聊 on `XtQuantLiveAdapter` reconnect exhausted.
- 钉聊 on event drop count > N.

## Notifications

When the drawdown kill switch fires (CLAUDE.md 「回撤 ≥ 5% 暂停」),
the runner invokes ``session_cfg.notify_fn(title, body)`` exactly
once per session — at the moment the flip from inactive to
active happens. Pass ``ops.notify.ding`` in production; pass a
spy closure in tests. ``None`` (default) keeps the WARN-log-
only behavior.

钉聊 activation is controlled by the ``DINGTALK_WEBHOOK_URL``
env var (see ``ops/notify.py``). The runner never raises if the
webhook is missing — the alert is best-effort.

### 钉聊 recipe

```python
import os
from execution import run_paper_session, PaperSessionConfig
from execution.bridge import AkquantStrategyCallable
from research.strategies.ma_cross import MACrossStrategy
from ops.notify import ding

if os.environ.get("DINGTALK_WEBHOOK_URL"):
    notify_fn = ding
else:
    def notify_fn(title, body): pass  # no-op in dev

bridge = AkquantStrategyCallable(MACrossStrategy)
report = run_paper_session(
    strategy=bridge, data=bars,
    adapter=AkquantPaperAdapter(), journal=PaperJournal(...),
    session_cfg=PaperSessionConfig(notify_fn=notify_fn),
)
```

### Alert body format

```
drawdown_pct=6.00%
kill_switch_cap=5.00%
cash=950000
positions_value=0
total_equity=950000
timestamp=2024-09-02T15:00:00
```

Plain text, one field per line. Stable format — Phase 5 parsers
can extract fields via simple regex without JSON parsing.
