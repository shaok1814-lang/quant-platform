# A-Share Rules Patch Layer

!!! info "Source file"
    This page imports [`backtest/a_share/README.md`](https://github.com/shaok1814-lang/quant-platform/blob/master/backtest/a_share/README.md) verbatim. Edit the source.

!!! note "One-line summary"
    AKQuant only enforces `ChinaStockConfig(tick_size=True)` out of the box; price-limits / suspension / ex-div / ST / survivorship-bias all require a self-research patch layer — 8 pure-function modules, each with ≥6 boundary unit tests.

--8<-- "a-share-rules-content.md"