---
name: forecast-task-planner
description: 当需要把自然语言 ask 转成预测实验评测意图时使用。典型触发包括“分析这次预测训练效果”“帮我定位 forecast badcase”“根据实验目录生成评测任务”。不用于通用写作、普通代码格式化、或不涉及预测实验评测的任务。
---

# 目的

把 `ask` 转成结构化评测意图：评测什么、重点看什么、需要哪些证据。不判断模型好坏，不猜测文件路径。

# 输入

可接受输入：
- 用户的自然语言 `ask`
- 实验目录路径
- 可选 `model_type`
- 目录扫描、日志解析、代码分析的后续结构化结果

仅在以下情况追问：
- `ask` 为空或完全无法判断是否是预测实验评测
- 实验目录路径缺失
- 用户要求执行训练、改代码、接数据库等 MVP 不支持的动作

# 工作流

1. 判断 `ask` 是否属于预测实验评测。
2. 提取关注点：日志、代码、指标、badcase、异常、建议、报告。
3. 如果 ask 中包含本地项目路径或实验目录路径，只把它记录为 `candidate_experiment_dirs`；不得把该路径改写成 prediction、actual 或 log 路径。
4. 未指定模型类型时写 `unknown`，不要编造。
5. 生成 `auto_forecast_task.yaml`。
6. `need_full_evaluation` 固定写 `auto`，由后续产物发现决定模式。

# 规则

- 只生成任务意图，不做效果判断。
- 不要虚构 prediction、actual、log、metrics 路径。
- 不要假设日志、预测结果或真实值一定存在。
- 不要触发训练、代码修改、数据库访问或前端生成。
- 如果用户要求 badcase，但缺少预测和真实值，仍可记录该意图，后续进入 Diagnostic 模式说明数据不足。
- 只要 `ask` 命中任一 forecast 场景，默认把 `optimization_suggestion` 放入 `focus`，除非用户明确要求只输出原始产物或不要建议。
- 报告必须由用户明确要求或完整分析交付需要触发；不要把建议请求自动升级成报告。
- 报告和建议都必须由后续结构化证据支撑。

# 输出格式

输出：

```yaml
task_type: forecast_evaluation
ask: "<original user ask>"
model_type: "<provided or unknown>"
candidate_experiment_dirs: []
focus:
  - log_analysis
  - code_analysis
  - evaluation
  - badcase
  - optimization_suggestion
need_code_analysis: true
need_log_analysis: true
need_full_evaluation: auto
unsupported_requests: []
```

# 易错点

- “定位 badcase”不等于一定能定位；只有 prediction 和 actual 可用时才能输出 badcase 明细。
- “训练效果”不等于可以直接说好坏；Diagnostic 模式只能说证据不足。
- 用户的业务名词可以进入 `model_type` 或 `focus_note`，但不要把业务名词当文件名。
- 如果 ask 里出现“上线”“优于某方案”等表达，先记录为关注点，不要给结论。

# 运行资源

- 执行 `scripts/validate_task_intent.py` 校验任务 YAML；不要读取脚本正文，除非脚本失败需要调试。
- 自建 agent 框架可选使用 `scripts/skill_loader.py` 读取本地 skills，用 `scripts/llm_planner.py` 生成工具计划；LLM 不可用时必须走 fallback 计划。
- `scripts/llm_client.py` 只作为可选 OpenAI 适配层，不替代 Codex 当前工具调用。
- 只有调整触发覆盖时读取 `references/eval_cases.md`。

# 质量检查

最终输出前确认：

- `auto_forecast_task.yaml` 没有虚构路径。
- `need_full_evaluation` 是 `auto`，不是提前强制 full。
- 输出只包含任务意图和关注点，没有模型效果结论。
- 不包含基线对比结论。
