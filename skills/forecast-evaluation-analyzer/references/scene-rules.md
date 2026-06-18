# 场景拆解规则

基础场景：

- `weekday`
- `weekend`
- `high_target`
- `normal_target`
- `low_target`
- `overestimate`
- `underestimate`

可选字段触发：

- 有 `is_holiday` 时生成 `holiday`
- 有 `is_peak_day` 时生成 `peak_day`
- 有 `is_long_tail` 时生成 `long_tail`
- 有 `history_days` 时生成 `short_history`

解释规则：

- 场景样本量过小时，只提示风险，不下强结论。
- 场景标签由脚本生成，不由 LLM 手动分配。
- 历史产物中的 `high_sales`、`normal_sales`、`low_sales` 仅作为旧标签兼容；新报告统一解释为目标值分层，不绑定具体业务含义。
