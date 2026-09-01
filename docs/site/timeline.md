# 项目时间线

6 周从零到 P3 全闭环。下表汇总每周的关键交付 + 测试数累计。

| 阶段 | 周 | 主题 | 关键模块 | 测试数累计 |
| | --- | --- | --- | --- |
| **P1 W1** | 1 | AKQuant 验证 + MA-cross 烟雾测试 | `research/strategies/ma_cross.py` | ~48 |
| **P1 W2** | 2 | 数据层（akshare + Parquet + DuckDB + 跨源校验） | `data_layer/`, `ops/cross_source_job.py` | ~100 |
| **P2 W3** | 3 | 因子库 + 策略集成 | `research/factor_lib/`（4 家族）, `research/strategies/`（2 真策略） | ~148 |
| **P2 W4** | 4 | A 股规则补丁层（8 规则全覆盖） | `backtest/a_share/` | ~250 |
| **P2 W5** | 5 | Walk-Forward + Optuna + 参数敏感度 | `research/factor_lib/analytics/` | ~330 |
| **P3 W6** | 6 | 自动 ingest + Dashboard + 跨源 audit + 钉聊告警 | `ops/ingest_job.py`, `ops/dashboard.py`, `ops/notify.py`, `ops/scheduler.py` | ~418 |
| **P3 W7.1** | 7 | 执行层（paper + xtquant live + 桥接 + 模拟盘纪律） | `execution/`（protocol / risk / runner / brokers / journal / bridge）, `ops/weekly_paper_job.py`, `scripts/run_paper_validation.py` | **537** |

!!! abstract "怎么读这张表"
    - **测试数累计** 不是 5 个新测试写完就 +5 —  包含修 bug、refactor、新模块、旧模块的额外边界 case 累计。`pytest -q` 跑全套 ~30 秒。
    - **关键模块** 列只挑该周新增 / 升级的代码路径，不是该周的全部工作。
    - 完整 commit history + 每个 commit 的设计决策 + bug post-mortem 在 [GitHub commit log](https://github.com/shaok1814-lang/quant-platform/commits/master)。

## 为什么这是真实的项目（vs 教程 demo）

!!! tip "几个 537 这个数字背后的细节"
    - **从 48 → 537 是真实的 6 周弧线**，不是一次写出来的。中间遇到 ~5 个真实 bug post-mortem（W2.1 akshare 代理问题，W3 IntParam 命名陷阱，W4 A-share 规则与 AKQuant 实际实现的差异，W6.3 aksak ql vs baostock qfq drift 67-363 bps，W7.1 cost-basis paper mode 下 max_drawdown_pct = 0% 的歧义）。
    - **CLAUDE.md 的 8 条硬约束 4 条都有专门的 enforcement 代码**（10% 仓位 cap → `execution/risk.py:check_position_cap`，5% drawdown kill → `execution/risk.py:check_drawdown_kill_switch`，4 周模拟盘纪律 → `ops/weekly_paper_job.py` + `ops/scheduler.py`，防过拟合 → `research/factor_lib/analytics/`）。
    - **不修改 AKQuant 源码**（CLAUDE.md 核心架构决策）— 所有扩展通过继承 / 包装 / 补丁层实现。`execution/brokers/akquant_paper.py` 包装 AKQuant `MiniQMTTraderGateway` stub 是最显式的例子。

## 下一步

- **4 周模拟盘纪律正式开始** — `python scripts/run_paper_validation.py` pre-flight → `python -m ops` launch。Sunday 9:00 CST 自动 paper session，4 周后首次评估 paper vs live deviation。
- **CLAUDE.md「单策略初始实盘资金不超过总资金 10%」** — 等 paper vs live deviation < 5% 后启动实盘，第一笔资金 ≤ 总资金 10%。
- **更多策略 / 因子** — 现在有 5 策略 × 4 因子家族，下一步可以加 Donchian System 2（pyramiding + ATR sizing）、Dual Momentum、pairs trading 等。