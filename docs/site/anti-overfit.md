# 防过拟合原则

!!! abstract "为什么需要这一页"
    CLAUDE.md 把防过拟合列为 **硬约束** — 这是 backtesting 圈最常见的过度自信陷阱 (curve fitting, lookahead bias, parameter over-tuning)。本页是 4 条规则 + 6 条禁令 + 本项目在代码里如何 enforce 的对照表。

--8<-- "anti-overfit-content.md"

---

## 下一步

- Walk-Forward 窗口的视觉效果 → [系统架构](architecture.md#walk-forward-windows)
- 怎么跑模拟盘 → [模拟盘手册](paper-runbook.md)
- 5 个策略长什么样 → [功能一览](features.md)
- 每个模块的公开 API → [API 参考](api-reference.md)