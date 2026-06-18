---
name: forecast-evaluation-analyzer
description: 当 prediction 和 actual 均可用，且需要评测预测指标、场景误差或异常时使用。典型触发包括“计算这次预测 WAPE/MAPE”“做预测评测”“按场景拆解预测误差”。不用于缺少 prediction 或 actual 的诊断任务、通用数据分析、或训练代码修改。
---

# 目的

在 Full 模式下执行脚本计算整体指标、场景指标和异常摘要。LLM 只能解释结果，不能手算或改写数值。

# 输入

可接受输入：
- prediction 文件路径
- actual 文件路径
- 可选 `model_type`
- 日志摘要、代码摘要、artifact summary
- 用户 `ask`

仅在以下情况追问：
- 用户强制 `--mode full`，但 prediction 或 actual 不存在
- prediction/actual 字段完全无法匹配

# 工作流

1. 确认 prediction 和 actual 都存在，且 artifact 不 ambiguous。
2. 执行 `scripts/calculate_metrics.py` 计算 WAPE、MAPE、Bias、MAE、RMSE。
3. 执行 `scripts/tag_scenes.py` 生成基础场景拆解。
4. 执行 `scripts/detect_anomalies.py` 识别高误差、系统偏差、零预测异常、日志异常等。
5. 将输出写入 `metrics_summary.csv`、`scene_metrics.csv`、`anomaly_summary.json`。
6. 将指标、场景和异常摘要交给 `forecast-optimization-advisor`，生成指标复核、场景拆解或后续实验建议。

# 规则

- 缺 prediction 或 actual 时停止完整评测，进入 Diagnostic 模式。
- LLM 不计算指标，不四舍五入改写源数值。
- 缺少可匹配 key 时，不硬拼数据，报告说明字段无法匹配。
- 不输出基线对比结论。
- 指标解释必须引用 `metrics_summary.csv` 或 `scene_metrics.csv` 中的值。
- 评测建议必须引用指标、场景或异常摘要，不要脱离数据泛泛建议。

# 输出格式

输出：

```text
metrics_summary.csv
scene_metrics.csv
anomaly_summary.json
```

Minimum metrics:

```text
wape
mape
bias
mae
rmse
rows
```

# 易错点

- actual 为 0 时 MAPE 需要跳过或按工具规则处理，不能手算。
- Bias 为正/负只表示方向性偏差，需要结合工具定义解释。
- 场景拆解样本量太小时，只能提示风险，不要下强结论。
- Full 模式允许评价效果，但必须基于数据产物。

# 运行资源

- 执行 `scripts/calculate_metrics.py` 计算整体指标；不要读取脚本正文，除非脚本失败需要调试。
- 执行 `scripts/tag_scenes.py` 计算场景指标。
- 执行 `scripts/detect_anomalies.py` 生成异常摘要。
- 解释指标含义时读取 `references/metrics.md`。
- 解释场景标签时读取 `references/scene-rules.md`。
- 只有调整触发覆盖时读取 `references/eval_cases.md`。

# 质量检查

最终输出前确认：

- prediction 和 actual 均存在且不 ambiguous。
- `metrics_summary.csv` 已生成后才解释指标。
- 指标数值来自文件，不是 LLM 推断。
- 报告不包含基线对比内容。
- 如输出建议，建议已引用 `metrics_summary.csv`、`scene_metrics.csv` 或 `anomaly_summary.json`。
