# 模拟盘手册

!!! info "本页面源文件"
    正文来自 [`docs/paper_validation_runbook.md`](https://github.com/shaok1814-lang/quant-platform/blob/master/docs/paper_validation_runbook.md) — CLAUDE.md 「实盘前必须经过至少 4 周模拟盘验证」硬约束的 operator 流程。

!!! tip "配套脚本"
    [`scripts/run_paper_validation.py`](https://github.com/shaok1814-lang/quant-platform/blob/master/scripts/run_paper_validation.py) 提供 6 项 pre-flight check + 可选的 `--smoke --weeks N` warm-up。

--8<-- "paper-runbook-content.md"

---

## 下一步

- 怎么跑 → [快速开始](quickstart.md)
- Kill-switch 的 enforce 细节 → [执行层架构](architecture.md#live-gateway)
- 防过拟合 4 条规则 → [防过拟合](anti-overfit.md)
- 框架选型理由 → [框架调研](framework-survey.md)