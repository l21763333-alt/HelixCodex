---
name: forecast-report-writer
description: 当需要基于预测实验的结构化证据生成评测报告或诊断报告时使用。典型触发包括“生成评估报告”“把这次实验分析写成 report.md”“根据 report_context 输出报告”。不用于没有实验上下文的通用写作、单独优化建议、营销文案、或非预测评测报告。
---

# 目的

基于 `report_context.json` 组织中文评测报告，作为 `final_report.md` 的附录证据来源。可以优化表达，不能修改数值、虚构证据或绕过 Diagnostic 限制。

注意：`final_report.md` 不再复用本技能的完整评测结构。最终报告必须由 `core/reports.py` 生成“实验验证结论报告”，只回答新方案是否有效、当前最佳方案是 baseline 还是 modified、失败或提升原因和下一步建议。

# 输入

可接受输入：
- `report_context.json`
- `auto_forecast_task.yaml`
- `artifact_summary.json`
- `code_analysis.json`
- `log_summary.json`
- 可选 `metrics_summary.csv`、`scene_metrics.csv`、`badcases.csv`、`anomaly_summary.json`

仅在以下情况追问：
- 没有 `report_context.json`，且无法从已有输出重建
- 用户要求报告包含 MVP 不支持内容

# 工作流

1. 读取 `report_context.json`。
2. 判断当前模式是 Full 模式还是 Diagnostic 模式。
3. Full 模式：写问题定位、任务理解、扫描文件样例、日志/代码分析、数据可用性、中文指标解释、场景拆解解释、badcase 解释、具体特征实验建议和下一步动作，作为评测证据附录。
4. Diagnostic 模式：写任务理解、扫描结果、日志/代码分析、缺失产物说明、已有证据、建议和下一步动作。
5. 写 `forecast_report.md` 或 `evaluation_report.md`；如果上下文没有建议，先使用 `forecast-optimization-advisor` 生成建议再整合进报告。
6. 执行报告校验脚本，确认没有禁用内容。

# 规则

- 报告只基于报告上下文。
- 报告必须尊重 `experiment_plan.yaml` 中 `evaluation_metric` 的用户目标和源码指标解释；建议步骤、实验决策说明和下一步动作都要围绕用户 ask 指定的目标。
- 不得修改指标数值。
- 不得添加不存在的文件路径、日志片段或 badcase。
- Diagnostic 模式必须说明无法重新计算 WAPE/MAPE/Bias。
- Diagnostic 模式不得输出“模型效果很好”“模型效果很差”“可以上线”等确定性结论。
- 不得出现基线对比结论，例如“优于 baseline”“低于 baseline”“baseline_wape”；允许在 MVP 边界中说明“不做基线对比”。
- 不要把单独的建议请求升级成报告；单独建议交给 `forecast-optimization-advisor`。
- `## 10. 优化建议` 必须优先整合 `forecast-optimization-advisor` 生成的建议；有 scene_metrics 或 badcase 证据时，建议必须具体到特征名、构造口径和验证指标，例如扩展已有 `rolling_windows`、构建源码可接入的 `recent_trend_ratio`、或验证已有 lifecycle 字段的分桶交叉。禁止只写“围绕某方面构造特征”“针对高误差场景优化”，也禁止把未被源码消费的空开关描述成已执行实验。

# 输出格式

输出完整评测附录，不作为最终实验决策报告：

```markdown
# 预测实验评测报告

## 1. 一句话结论
## 2. 任务理解
## 3. 实验目录扫描结果
## 4. 日志解析结果
## 5. 代码结构与实验执行说明
## 6. 评测数据可用性
## 7. 整体指标
## 8. 细粒度效果拆解
## 9. badcase 定位
## 10. 优化建议
## 11. 下一步动作
```

# 易错点

- Diagnostic 报告不是失败报告；它应该清楚说明哪些证据可用、哪些结论不能下。
- Full 报告可以评价效果，但要引用指标和 badcase。
- “未找到日志”要说明是没有可解析日志路径或日志 artifact，不得写成“模型能力不可用”，也不要写成训练无异常。
- “未找到代码”不影响数据评测，但要说明代码结构分析不可用。
- 一句话结论不要写运行模式；必须写主要问题定位和建议解决方向。
- 实验目录扫描结果必须展示扫描文件样例；如果文件数明显偏少，要提示用户检查 `--experiment` 是否指向 toy fixture 或子目录。
- 指标名称必须附中文解释，例如 WAPE 是主指标、Bias 是整体偏差。
- 细粒度场景标签必须解释 weekday/weekend、high_target/normal_target/low_target、underestimate/overestimate 等含义；如遇旧标签 high_sales/normal_sales/low_sales，只按目标值分层兼容解释，不绑定具体业务场景。
- badcase 必须解释为误差样本定位工具，不得只列英文字段。
- 如果有 `experiment_plan` 和 `run_status`，必须说明原实验入口、trial 训练副本、特征改动、训练数据/配置清单和输出目录。
- 若 `run_status.output_contract` 存在，报告中的预测文件、split 口径、指标列和辅助指标名称必须读取该 contract，不要写死 `package_detail` 或 `store_dish_day`。
- 如果有 `best_trial_review.json`，最终多轮报告必须说明这是 Agent2 读各 trial 结果后的最佳方案复核，而不是 Python 固定规则直接指定。
- 优化建议不能停留在“复核高误差场景”“围绕 badcase 做特征实验”；只要有场景或 badcase 证据，就要输出 Agent2 可执行的候选实验，例如滚动统计、趋势比值、生命周期分桶、结束窗口分桶、分层校准等，并说明字段口径。
- 不要把完整评测报告直接拼进 `final_report.md`；最终报告只能引用本报告路径作为附录产物。

# 运行资源

- 定稿前执行 `scripts/validate_report.py`；不要读取脚本正文，除非脚本失败需要调试。
- 自建 agent 框架可选使用 `scripts/build_report_context.py` 汇总结构化产物；`scripts/write_report.py` 只能作为本地模板校验/测试 helper，runtime 不得把它作为报告生成 fallback。
- 报告正文必须由 Agent1 LLM 基于 `report_context.json` 自由生成并受本技能规则约束；LLM 不可用、返回空内容或校验失败时，本轮运行必须 fail fast 并写审计错误，不得生成确定性 fallback 报告。
- 写报告结构时读取 `references/report-template.md`。
- 只有调整触发覆盖时读取 `references/eval_cases.md`。

# 质量检查

最终输出前确认：

- Diagnostic 模式包含固定说明：由于缺少 prediction 或 actual，本次无法重新计算 WAPE/MAPE/Bias。
- 报告没有确定性上线/好坏结论，除非 Full 模式有数据证据。
- 报告没有虚构路径或证据。
- 报告不包含基线对比结论。
