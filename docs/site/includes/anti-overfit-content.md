## 防过拟合原则（必须遵守）

所有策略在交付前必须通过：

1. **Walk-Forward 验证**：训练 2 年、测试 1 年、季度滚动
2. **样本内外对比**：测试集表现衰减 < 30%
3. **参数敏感度**：最优参数附近 ±20% 范围内表现稳定
4. **简单优先**：同等收益下选择参数更少的策略

**禁止**：

- ❌ 在全样本上优化后直接报告 Sharpe
- ❌ 用未来数据做特征工程
- ❌ 只展示收益最好的几次回测
- ❌ 把过拟合的策略包装成"已验证"

## 本项目如何落实

四条规则在代码里有具体执行点：

| 规则 | 实现 |
| |  |
| --- | --- |
| Walk-Forward 2/1/季度 | `research/factor_lib/analytics/walk_forward.py::run_walk_forward(train_months=24, test_months=12, step_months=3)` — 拒绝 `step_months < test_months`（防止窗口重叠）。 |
| OOS 衰减 < 30% | `walk_forward.py::WalkForwardResult.is_to_oos_decay` 给出每个 metric 的 in-sample / out-of-sample 比值（higher-is-better ≥0.70，lower-is-better ≤1.30）。 |
| 参数敏感度 ±20% | `analytics/param_sensitivity.py::assert_stable` — 单参数 ±20% 网格扫描，metric 不超过 ±30%  才算稳定。 |
| 简单优先 | `optuna_runner.py` 只用 Optuna（CLAUDE.md 明确禁止 grid search），并把 `n_trials` 控制在小范围。 |
| 禁止未来数据 | 所有因子用 `pandas.shift(1)` 或 `rolling(...).iloc[-1]` 严格只用历史数据（factor_lib 单元测试覆盖）。 |
| 禁止"选最优样本" | `analytics` 模块统一用 `phase="is" / "oos"` 显式标签，不让 caller 偷偷挑样本。 |