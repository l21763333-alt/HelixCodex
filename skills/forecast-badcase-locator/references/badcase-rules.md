# Badcase 规则

badcase 类型：

- `top_abs_error`：绝对误差最大的样本
- `top_ape`：百分比误差最大的样本
- `top_underestimate`：低估最严重的样本
- `top_overestimate`：高估最严重的样本
- `extreme_underestimate`：低估方向的极端样本，默认 `ape > 0.2` 且 `abs_error > 10`
- `extreme_overestimate`：高估方向的极端样本，默认 `ape > 0.2` 且 `abs_error > 10`
- `consecutive_underestimate`：同一业务 key 在日期排序后连续低估，默认连续 `>=3` 条
- `consecutive_overestimate`：同一业务 key 在日期排序后连续高估，默认连续 `>=3` 条

解释规则：

- APE 在 `actual` 很小时会被放大，需要提醒样本基数影响。
- 不要把单个 badcase 写成全局根因。
- 如果缺少 业务对象、分组维度或日期字段，只使用文件中可用的 key 列。
- 不做基线相关 badcase。
- 极端 badcase 必须同时引用百分比误差和绝对误差阈值。
- 连续偏差必须引用 `streak_length`、`streak_start_date`、`streak_end_date`，不能直接解释成全局趋势。
