# 个人 A 股量化研究与交易系统

> 一个建在 AKQuant 之上的自研 A 股量化平台 — 数据完备、规则完备、防过拟合纪律完备、模拟盘跑通。

[查看功能 :material-arrow-right:](features.md){ .md-button .md-button--primary }
[快速开始 :material-rocket-launch:](quickstart.md){ .md-button }
[GitHub 仓库 :fontawesome-brands-github:](https://github.com/shaok1814-lang/quant-platform){ .md-button }

---

## 这是什么

quant-platform 是一个 **个人 A 股量化研究与交易系统**。选 [AKQuant](https://github.com/) 作为回测 / 撮合引擎骨架（CLAUDE.md 的核心架构决策），其上自研 A 股规则补丁层、因子库、执行层、ops 监控。

项目目标不是「又一套量化框架」，而是把 [CLAUDE.md](https://github.com/shaok1814-lang/quant-platform/blob/master/CLAUDE.md) 里的硬约束 ——

> 单策略初始实盘资金不超过总资金 10%；实盘前必须经过至少 4 周模拟盘验证；所有实盘代码必须经过模拟盘对比验证偏差 < 5%

—— 在一个 537  单元测试的代码库里 **真的落地**。

## 一数字

<div class="stat-grid" markdown>
<div class="stat-tile" markdown>
<span class="value">5</span>
<span class="label">AKQuant 策略</span>
</div>
<div class="stat-tile" markdown>
<span class="value">8</span>
<span class="label">A 股规则补丁</span>
</div>
<div class="stat-tile" markdown>
<span class="value">537</span>
<span class="label">单元测试通过</span>
</div>
<div class="stat-tile" markdown>
<span class="value">7</span>
<span class="label">周从 0 到 P3</span>
</div>
</div>

<div class="teaser-grid" markdown>

<div class="teaser-card" markdown>
### [:material-shield-check: A 股规则完备](a-share-rules.md)

涨跌停 / 停牌 / 除权 / ST / 100 股整手 / 印花税 / 幸存者偏差 — 8 个独立 pure-function 模块，每个 ≥6 个边界单元测试。

</div>

<div class="teaser-card" markdown>
### [:material-test-tube: 防过拟合流程](anti-overfit.md)

Walk-Forward 24m/12m/3m + Optuna + ±20% 参数敏感度 + IS/OOS 衰减 < 30% — CLAUDE.md 的硬约束在代码里强制执行。

</div>

<div class="teaser-card" markdown>
### [:material-rocket-launch: 从模拟到实盘](paper-runbook.md)

Sunday 9:00 cron 自动跑 MACrossStrategy paper session + 钉聊 alert + dashboard drill-down。4 周模拟盘纪律自动化。

</div>

</div>

## 技术栈

| 维度 | 选型 |
| |  |
| --- | --- |
| 数据源 | akshare (主) · baostock (校验) · adata · tushare |
| 存储 | DuckDB + Parquet，akshare `qfq` 前复权统一口径 |
| 引擎 | AKQuant（不修改源码，仅通过继承 / 包装扩展） |
| 因子 | pandas + numpy（不用 AKQuant polars DSL — 保留可调试性） |
| 参数优化 | Optuna（CLAUDE.md 明确禁止 grid search） |
| 调度 | APScheduler (daily 18:00 增量 + Sunday 9:00 paper) |
| 告警 | 钉钉 webhook (env-var gated, 默认 inactive) |
| Dashboard | Streamlit + Plotly |
| 测试 | pytest（537  个测试，~30s 全套） |

完整列表见 [技术栈](tech-stack.md)。