# A 股规则补丁层

!!! info "本页面源文件"
    本页面正文来自 [`backtest/a_share/README.md`](https://github.com/shaok1814-lang/quant-platform/blob/master/backtest/a_share/README.md) — 源文件更新后重新运行 `mkdocs build` 即可同步。

!!! note "一句话总结"
    AKQuant 默认只 enforce `ChinaStockConfig(tick_size=True)`，涨跌停/停牌/除权/ST/幸存者偏差 全部需要自研补丁层 — 8 个独立 pure-function 模块，每个都有 ≥6 个边界单元测试。

--8<-- "a-share-rules-content.md"