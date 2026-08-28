# W4 A-Share Rules Patch Layer

This package ships the **A-share boundary list** that `CLAUDE.md` mandates every strategy must explicitly handle. AKQuant ships only `ChinaStockConfig(enforce_tick_size=True)` (a closed dataclass with one field); every other rule is layered here as a pure-function utility.

## Rule coverage matrix

| Rule | Module | AKQuant also? |
|---|---|---|
| T+1 交割 | (use `t_plus_one=True`) | yes |
| 涨跌停 | `price_limits` | no |
| 停牌 | `suspension` | no |
| 除权除息 (qfq) | `ex_dividend` | data-layer qfq (W2) |
| ST 股票过滤 | `st_filter` | no |
| 100 股整手 | `lot_enforcement` | yes (buy-side strict; `close_position` bypasses) |
| 印花税卖单边 | `stamp_tax` | yes |
| 幸存者偏差 | `delisted_universe` | no |

## Per-rule cookbook

### 1. 涨跌停 (`price_limits`)

```python
from backtest.a_share.price_limits import compute_limit_price, is_limit_up

prev_close = 10.00
bounds = compute_limit_price(prev_close, is_st=False, board="main")
# LimitBounds(lower_limit=8.99, upper_limit=11.00)

is_limit_up(11.00, prev_close, is_st=False, board="main")  # True
is_limit_up(11.005, prev_close, is_st=False, board="main")  # False (not at 0.01 boundary)
```

In a strategy `on_bar`:

```python
def on_bar(self, bar):
    df = self.get_history_df(count=2)
    if df.empty:
        return
    prev_close = float(df["close"].iloc[-2])
    if is_limit_up(float(bar.close), prev_close, is_st=False, board="main"):
        return  # 涨停日不可买入
```

### 2. 停牌 (`suspension`)

```python
from backtest.a_share.suspension import infer_suspension_from_ohlcv

mask = infer_suspension_from_ohlcv(bars_df)  # pd.Series[bool] aligned to bars_df.index
```

Detection rule (best-effort heuristic — no akshare daily-suspension endpoint):
- `volume == 0` ⇒ True (无成交)
- OR consecutive bars with `high == low == close == prev_close` ⇒ True (flat-line / 一字板 / 停牌)

> **Limitation**: false negatives (a bar with volume > 0 but the symbol actually halted) cannot be detected without an authoritative calendar. W5+ may integrate Tushare's `suspend_d` endpoint if a token is available.

### 3. 除权除息 (`ex_dividend`)

```python
from backtest.a_share.ex_dividend import detect_ex_dividend_days

ex_div_dates = detect_ex_dividend_days(bars_df)  # bars_df must have 'adj_factor' col
```

The data layer's qfq-adjusted bars are the canonical source (W2). This detector is a backtest-layer mirror that flags rows where the qfq adjustment factor jumps — useful for sanity-checking data-layer consistency.

### 4. ST 过滤 (`st_filter`)

```python
from backtest.a_share.st_filter import fetch_st_symbols, filter_st

st_set = fetch_st_symbols(allow_network=False, offline_csv="data/st_a_share_list.csv")
universe = filter_st(["000001", "600000", "600519"], include_st=False, st_set=st_set)
# Default include_st=False drops ST symbols per CLAUDE.md.
```

`fetch_st_symbols` wraps akshare `stock_zh_a_st_em()` (point-in-time snapshot — historical ST membership is NOT exposed by akshare).

### 5. 100 股整手 (`lot_enforcement`)

```python
from backtest.a_share.lot_enforcement import enforce_lot, is_valid_lot

enforce_lot(150)   # 100 (round down)
enforce_lot(250)   # 200
is_valid_lot(100)  # True
is_valid_lot(150)  # False
```

AKQuant already enforces lot size at order time (buy-side strict; `close_position` deliberately bypasses for clean unwinds). This module is the strategy-side / pre-check equivalent.

### 6. 印花税卖单边 (`stamp_tax`)

```python
from backtest.a_share.stamp_tax import compute_stamp_tax

compute_stamp_tax(100_000.0, side="buy")   # 0.0
compute_stamp_tax(100_000.0, side="sell")  # 100.0 (rate=0.001 default)
```

AKQuant's `stamp_tax_rate` is already sell-only. This is the pure-function equivalent for offline checks / tests.

### 7. 幸存者偏差 (`delisted_universe`)

```python
from backtest.a_share.delisted_universe import fetch_delisted_symbols, build_universe

delisted_set = fetch_delisted_symbols(allow_network=False, offline_csv="data/delisted_a_share_list.csv")
universe = build_universe(
    ["000001", "600000", "600001"],  # 600001 is delisted
    include_delisted=True,           # default per CLAUDE.md
    delisted_set=delisted_set,
)
# All 3 retained; setting include_delisted=False drops 600001 (survivor bias).
```

## Self-attestation: `AShareRuleChecklist`

Per CLAUDE.md "每次写新策略前,列出涉及到的规则清单", new strategies should fill an `AShareRuleChecklist` in `on_start` to declare which rules they handle. This is **advisory** (no auto-enforcement) but documented here for reviewer visibility:

```python
from backtest.a_share import AShareRuleChecklist

class MyStrategy(akquant.Strategy):
    def on_start(self):
        self._checklist = AShareRuleChecklist(
            price_limits_checked=True,
            suspension_checked=True,
            ex_dividend_checked=True,
            st_filter_applied=True,
            delisted_universe_used=True,
            lot_enforced=True,
            stamp_tax_acknowledged=True,  # sell-only, AKQuant built-in
        )
```

W3 strategies (`ma_cross.py`, `ma_cross_duckdb.py`, `topn_mean_reversion.py`, `factor_timing.py`) currently do NOT instantiate the checklist; they continue to use AKQuant's defaults only. A separate W4.1 commit can opt them in incrementally.

## Out of scope (W4 does NOT do)

- Modify AKQuant source (禁 per CLAUDE.md).
- Change W3 strategy constants / wiring.
- Walk-forward validation (W5).
- Industry neutralization (W5).
- Live trading / miniQMT (P3).
- Per-symbol daily ST membership history (no akshare endpoint; use point-in-time snapshot).
- Daily suspension calendar (no akshare endpoint; W4 uses OHLCV inference).
- Static-checker for `AShareRuleChecklist` (W4.1+).