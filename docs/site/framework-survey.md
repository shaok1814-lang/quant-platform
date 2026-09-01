# 框架调研

!!! info "本页面源文件"
    正文来自 [`docs/量化开源框架调研与落地方案.md`](https://github.com/shaok1814-lang/quant-platform/blob/master/docs/量化开源框架调研与落地方案.md) — 项目启动时对 17 个开源量化框架的全量调研 + 落地方案论证。

!!! abstract "TL;DR"
    17 个候选框架 (Qlib / backtrader / vectorbt / rqalpha / AKQuant / NautilusTrader / WonderTrader 等) 经过 6 维度对比，最终选 **AKQuant 作核心引擎 + 自研模块补足** 的分层架构。具体不选原因见 CLAUDE.md §架构决策 与本页正文。

--8<-- "framework-survey-content.md"

---

## 下一步

- 架构总览 → [系统架构](architecture.md)
- 17 个候选的最终决定 → CLAUDE.md §架构决策
- 功能速览 → [功能一览](features.md)
- 项目怎么从 W1 走到 W7.1 → [项目时间线](timeline.md)