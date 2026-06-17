---
name: forecast-badcase-locator
description: 当 prediction 和 actual 均可用，且需要定位预测 badcase、最大误差样本、低估样本、高估样本、极端偏差或连续同方向偏差时使用。典型触发包括“定位 badcase”“找预测误差最大的 业务对象/日期/分组维度”“分析哪些样本低估高估最严重”“是否连续低估/高估”。不用于缺少 prediction 或 actual 的诊断任务、普通日志分析、或与预测误差无关的排查。
---

# 目的

在 Full 模式下执行脚本对样本级误差排序，输出可复核的 badcase 明细。LLM 只解释现象，不排序或手算误差。

# 输入

可接受输入：
- prediction 文件路径
- actual 文件路径
- 已合并的评测数据或指标输出
- 可选场景指标、异常摘要
- 用户 `ask`

仅在以下情况追问：
- 用户要求定位特定业务维度，但数据里没有相关字段
- prediction/actual 无法合并

# 工作流

1. 确认当前模式是 Full 模式。
2. 确认 prediction 和 actual 均存在且不 ambiguous。
3. 执行 `scripts/mine_badcases.py`。
4. 生成基础四类 badcase：`top_abs_error`、`top_ape`、`top_underestimate`、`top_overestimate`。
5. 生成极端 badcase：默认 `APE > 20%` 且 `abs_error > 10`，按方向输出 `extreme_underestimate`、`extreme_overestimate`。
6. 如存在日期字段，检查同一业务 key 下连续同方向偏差：默认连续 `>=3` 条，输出 `consecutive_underestimate`、`consecutive_overestimate`。
7. 报告中引用 badcase 的样本字段、误差方向、误差大小；连续偏差还要引用起止日期和连续长度。
8. 将 `badcases.csv` 和可用场景指标交给 `forecast-optimization-advisor`，生成分层复核、样本核查或数据口径验证建议。

# 规则

- 缺 prediction 或 actual 时不要输出 badcase 明细。
- badcase 排序、极端筛选和连续偏差识别必须由脚本完成。
- 极端 badcase 默认同时满足百分比偏差和绝对偏差阈值：`ape > 0.2` 且 `abs_error > 10`。
- 连续偏差仅在存在日期字段时判断；按同一业务 key、日期排序后连续同方向误差默认 `>=3` 条。
- 不要把单个 badcase 直接说成全局根因。
- 不要输出“可上线/不可上线”这类结论。
- 不做基线相关 badcase。
- badcase 建议必须围绕样本复核、分层检查和证据补充，不把局部样本直接升级成全局结论。

# 输出格式

输出：

```csv
badcase_type,key_columns,date,prediction,actual,error,abs_error,ape
top_abs_error,...
top_ape,...
top_underestimate,...
top_overestimate,...
extreme_underestimate,...
extreme_overestimate,...
consecutive_underestimate,...
consecutive_overestimate,...
```

连续偏差类型额外输出：`streak_length`、`streak_start_date`、`streak_end_date`、`streak_abs_error_sum`。

# 易错点

- APE 在 actual 很小时会被放大，解释时要提醒样本基数影响。
- 最大绝对误差和最大百分比误差可能不是同一批样本。
- 低估/高估方向必须以工具字段为准，不能凭直觉反过来解释。
- 极端 badcase 需要同时满足百分比和绝对值阈值，避免小基数百分比异常被单独放大。
- 连续偏差是同一业务 key 在日期排序后的连续同方向误差，不等同于全局趋势或根因。
- 如果数据缺少 业务对象/分组字段，只能使用可用 key 列描述样本。

# 运行资源

- Full 模式且 prediction/actual 可用时执行 `scripts/mine_badcases.py`；默认阈值为 `--extreme-ape-threshold 0.2`、`--extreme-abs-error-threshold 10`、`--min-streak 3`；不要读取脚本正文，除非脚本失败需要调试。
- 解释 badcase 类型时读取 `references/badcase-rules.md`。
- 只有调整触发覆盖时读取 `references/eval_cases.md`。

# 质量检查

最终输出前确认：

- `badcases.csv` 只在 Full 模式生成。
- 基础四类 badcase 类型齐全，除非数据不足。
- 极端 badcase 已按正向/负向偏差区分；如没有输出，说明没有样本同时超过两个阈值。
- 有日期字段时已检查连续正向/负向偏差；如没有日期字段，报告中说明无法判断连续性。
- 每条 badcase 解释都能追溯到 `badcases.csv`。
- 没有输出基线相关结论。
- 如输出建议，建议已引用 `badcases.csv` 中的 badcase 类型、样本字段或误差方向。
