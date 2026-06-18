---
name: using-forecast
description: 当用户要求分析、评测、诊断预测实验，扫描实验目录，计算预测指标，定位 forecast badcase，生成预测评估报告或优化建议时使用。
---

# Using Forecast

## 定位

本技能是 forecast 领域的流程调度器。它只决定调用哪些 `forecast-*` 技能、按什么顺序调用、何时进入 Full 或 Diagnostic 模式，以及如何在任何 预测场景后给出证据化的相应建议。

不要在本技能中替代叶子技能的职责：不扫描文件、不手算指标、不解析日志、不排序 badcase、不编写报告正文。

## 全局建议原则

只要用户请求命中任一 预测场景（分析、评测、诊断、扫描实验目录、计算指标、定位 badcase、生成报告、优化建议），在完成对应证据产出后，都应使用 `forecast-optimization-advisor` 给出相应建议，除非用户明确要求“只输出原始结果/不要建议”。

建议不等于报告：

- 扫描类请求：建议补齐、消歧或确认哪些产物。
- 日志/代码诊断：建议排查哪些 warning、error、入口或指标口径。
- 指标评测：建议按哪些场景复核、补充哪些异常解释或后续实验。
- badcase 定位：建议按哪些维度分层复核、抽样核查或补充数据。
- 报告请求：在报告中整合建议；可按需额外输出 `optimization_suggestions.md`。

不要为了给建议而强制生成评测报告。只有用户明确要求报告、完整实验分析产物，或当前任务需要沉淀 `report.md` 时，才调用 `forecast-report-writer`。

## 冲突处理

当本技能与某个 `forecast-*` 技能同时适用时：

1. 用户要求完整实验分析、训练效果诊断、评测报告、优化建议、或未明确限定为单点任务时，先使用本技能编排流程。
2. 用户只要求单点动作时，直接使用对应叶子技能，但叶子技能输出后仍要基于已获得证据调用 `forecast-optimization-advisor` 给出相应建议。
3. 本技能的流程规则只决定调用顺序和模式分支；具体执行规则以被调用的叶子技能为准。
4. 叶子技能的输入前置条件优先于本技能的期望流程。例如缺少 prediction 或 actual 时，不得强行调用完整评测或 badcase 定位。
5. 用户明确指令优先于本技能。如果用户禁止改代码、禁止执行脚本或要求只说明方案，必须遵守。

## 串行流程

完整或未限定范围的请求按以下顺序推进：

1. 使用 `forecast-task-planner` 把用户 ask 转成 `auto_forecast_task.yaml`。
2. 使用 `forecast-experiment-scanner` 扫描实验目录，输出 `artifact_summary.json`。
3. 使用 `forecast-code-log-analyzer` 分析可用日志和代码，输出 `log_summary.json` 和 `code_analysis.json`。
4. 根据 `artifact_summary.json` 判断 Full 或 Diagnostic 模式。
5. Full 模式下使用 `forecast-evaluation-analyzer` 生成整体指标、场景指标和异常摘要。
6. Full 模式且用户关注 badcase 时，使用 `forecast-badcase-locator` 生成 `badcases.csv`。
7. 使用 `forecast-optimization-advisor` 基于结构化证据生成 `optimization_suggestions.md`。
8. 仅当用户要求报告或需要报告产物时，使用 `forecast-report-writer` 基于 `report_context.json` 生成最终报告。

单点请求按以下顺序推进：

1. 使用对应叶子技能产出请求范围内的结构化证据。
2. 将可用证据传给 `forecast-optimization-advisor`。
3. 输出与该请求范围匹配的简短建议或 `optimization_suggestions.md`，不要扩展成完整报告。

## 模式判断

进入 Full 模式必须同时满足：

- prediction 文件存在。
- actual 文件存在。
- prediction 和 actual 均未标记为 ambiguous。
- prediction 和 actual 字段可以匹配。

任一条件不满足时进入 Diagnostic 模式。

Diagnostic 模式下：

- 不调用 `forecast-evaluation-analyzer` 重新计算 WAPE/MAPE/Bias。
- 不调用 `forecast-badcase-locator` 输出样本级 badcase 明细。
- 只基于已有日志、代码、metrics 文件和产物扫描结果做诊断。
- 报告必须说明缺少哪些证据，以及哪些结论不能下。

## 证据边界

- 所有结论必须来自结构化产物或叶子技能输出。
- 不要根据文件名、用户描述或经验猜测 prediction/actual 路径。
- 不要由 LLM 手算、四舍五入或改写指标数值。
- 不要把 warning 直接写成确定根因。
- 不要把单个 badcase 写成全局根因。
- 不要输出“可上线/不可上线”或无证据的模型好坏结论。
- 不要输出基线对比结论，除非后续专门增加支持该能力的技能和证据规范。

## 产物衔接

推荐把各步骤输出汇总为 `report_context.json`，供建议和报告阶段使用。上下文按可用性包含：

- `auto_forecast_task.yaml`
- `artifact_summary.json`
- `log_summary.json`
- `code_analysis.json`
- Full 模式下的 `metrics_summary.csv`、`scene_metrics.csv`、`anomaly_summary.json`
- Full 且需要 badcase 时的 `badcases.csv`
- 建议阶段输出的 `optimization_suggestions.md`
- 报告阶段按需输出的 `report.md`

如果某类产物不可用，在上下文中显式标记 unavailable 或 missing，不要省略后让后续步骤猜测。

单点任务可以只传入已产出的局部证据。例如只有 `artifact_summary.json` 时，建议只能围绕缺失产物、ambiguous 候选和下一步补证据展开。

## 停止条件

遇到以下情况先停止并向用户说明：

- 实验目录不存在。
- 用户提供的人工覆盖路径不存在。
- 用户要求训练模型、修改实验代码、接数据库或执行本 MVP 不支持的动作。
- 用户强制 Full 模式，但 prediction 或 actual 缺失。
- prediction/actual 多候选且 discovery 标记 ambiguous。
