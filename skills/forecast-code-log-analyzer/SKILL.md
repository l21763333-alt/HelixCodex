---
name: forecast-code-log-analyzer
description: 当需要基于预测实验代码和训练或评测日志分析训练流程、评估入口、指标定义、异常或 warning 时使用。典型触发包括“看一下训练日志有没有问题”“分析实验代码结构”“从日志和代码判断这次训练是否异常”。不用于普通代码重构、无日志的闲聊、或不涉及预测实验证据分析的任务。
---

# 目的

基于代码和日志生成证据化摘要：入口、指标函数、特征模块、模型模块、输出模式、error/warning、loss/metric、样本量和运行状态。

# 输入

可接受输入：
- `scan_result`
- `artifact_summary`
- 代码文件列表
- 日志路径或日志候选
- 用户 `ask`

仅在以下情况追问：
- 用户要求解释具体业务结论，但既没有日志也没有代码、也没有数据产物
- 用户要求修改实验代码

# 工作流

1. 如果找到日志，执行 `scripts/parse_log.py`。
2. 如果没有日志，生成 `log_summary.json`，标记 `available=false`。
3. 如果找到代码文件，执行 `scripts/analyze_code.py`。
4. 如果没有代码文件，生成 `code_analysis.json`，标记代码分析不可用。
5. 把日志异常和代码结构写入报告上下文，作为后续建议证据。
6. 如果用户需要诊断、分析或下一步建议，把日志和代码摘要交给 `forecast-optimization-advisor` 生成排查、口径确认或补证据建议。

# 规则

- 只基于实际代码和日志说话。
- 入口文件、依赖文件和输出表可能来自 model contract；分析时优先使用 scan_result、artifact_summary 或 execution plan 中的入口线索，不假设固定文件名。
- warning 是风险信号，不要直接说成根因。
- 没有 error 不等于训练一定成功。
- 有 error 也要说明证据来自日志，不要扩展成未验证的业务结论。
- 不要因为代码里出现 `wape` 就认为本次一定已计算 WAPE；只能说“代码中存在相关指标逻辑”。
- 不要修改实验代码。
- 仅有日志或代码证据时，建议不能评价预测效果，只能给排查和补证据动作。

# 输出格式

输出：

```json
{
  "code_analysis": {
    "available": true,
    "entrypoints": [],
    "metric_functions": [],
    "feature_modules": [],
    "model_modules": [],
    "output_patterns": [],
    "possible_issues": []
  },
  "log_summary": {
    "available": true,
    "errors": [],
    "warnings": [],
    "losses": [],
    "metrics": [],
    "row_counts": [],
    "status": "unknown"
  }
}
```

# 易错点

- `warning` 可能来自依赖库，不一定影响训练结果。
- `nan` 可能出现在文本字段、日志说明或指标值里，要结合上下文。
- notebook 代码不一定完整可执行，只能作为结构线索。
- 代码中存在 `to_csv` 不代表本次运行真的产出了文件。

# 运行资源

- 有日志路径时执行 `scripts/parse_log.py`；不要读取脚本正文，除非脚本失败需要调试。
- 有代码文件时执行 `scripts/analyze_code.py`。
- 只有新增日志关键词时读取 `references/log-patterns.md`。
- 只有新增代码启发式规则时读取 `references/code-patterns.md`。
- 只有调整触发覆盖时读取 `references/eval_cases.md`。

# 质量检查

最终输出前确认：

- 日志不可用时明确写出不可用，不失败。
- 代码不可用时明确写出不可用，不失败。
- 每个异常判断都有日志、代码或结构化字段支撑。
- 没有输出确定性模型好坏结论。
- 如输出建议，建议已引用日志或代码摘要中的具体证据。
