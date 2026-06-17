---
name: forecast-optimization-advisor
description: 当需要基于预测实验的扫描结果、日志、代码、指标、异常或 badcase 生成有证据的相应建议时使用。典型触发包括“给出优化建议”“这次实验下一步怎么改”“根据 badcase 提改进方向”“扫描后怎么办”。不用于没有证据输入的泛泛建议、自动训练请求、或要求直接修改实验代码。
---

# 目的

把任意可用的 forecast 结构化证据转成与当前任务范围匹配的可执行建议。每条建议必须引用证据，并表达为“验证/排查/补充”，不要伪装成确定根因。

# 输入

可接受输入：
- `report_context.json`
- `auto_forecast_task.yaml`
- `artifact_summary.json`
- 日志摘要
- 代码结构分析
- 整体指标
- 场景指标
- badcase 明细
- 异常摘要

仅在以下情况追问：
- 用户要求给确定根因，但当前证据不足
- 用户要求自动训练或修改实验代码

# 工作流

1. 从可用上下文中收集证据：扫描结果、日志、代码、指标、场景、badcase、异常。
2. 先读取用户 ask，判断用户真正要优化的业务目标、指标名称、方向和约束；不要把 WAPE 当成永远的主目标。
3. 生成 Agent1 评测方案时，由模型根据 ask、源码和证据输出主优化目标、源码指标定义、建议重点和理由。
4. 再判断证据范围：扫描可用、日志/代码可用、指标可用、badcase 可用、完整报告上下文可用。
5. 按用户目标和证据类型生成建议：产物补齐、数据质量、特征、目标定义、评估口径、样本覆盖、训练稳定性。
6. 建议粒度必须匹配请求范围：单点任务给对应建议，完整分析给综合建议。
7. Diagnostic 模式只给补证据和排查建议，不判断模型效果。
8. 输出简短建议；当需要产物沉淀或报告上下文时输出 `optimization_suggestions.md`。

# 规则

- 每条建议必须有证据。
- 用户 ask 是建议方向和评测口径的最高优先级输入；当 ask 指定了具体指标名（例如 `test_bias`、`bias_rate`、自定义业务指标）时，Agent1 必须先根据源码、评估脚本或日志确认该指标的计算公式，再写入 `experiment_plan.yaml` 的 `evaluation_metric`，不要凭指标名称猜它等价于 WAPE 或 signed Bias。
- `evaluation_metric` 必须区分 `objective_label` 和 `decision_metric`：前者是用户说的指标名，后者是源码公式映射后的实际比较字段；同时写明 `metric_definition_status`、`metric_formula`、`metric_definition_source` 和 `metric_mapping_reason`。
- 如果源码中找不到用户指标的定义，必须标记 `metric_definition_status: unresolved`，并输出需要补充的证据；不要让 Agent2 根据猜测选择最佳方案。
- 不要使用“根因一定是”“证明了”等确定性措辞。
- Diagnostic 模式不要说模型好坏。
- 不要建议自动训练、自动改代码或接数据库。
- 不要输出基线对比建议。
- 不要为了给建议而生成完整报告。
- 只有扫描证据时，建议只能围绕缺失产物、ambiguous 候选、路径确认和下一步评测准备。
- 只有日志或代码证据时，建议只能围绕排查、口径确认和证据补充，不评价预测效果。
- 有指标或 badcase 证据时，建议可以涉及分层复核、样本核查、特征/数据口径验证和后续实验设计，但必须优先服务用户 ask 中的优化目标。
- 有 scene_metrics 或 badcase 证据时，建议必须具体到可执行特征实验，给出明确特征名、构造口径和验证指标；不要只写“围绕某方面构造特征”。
- Agent1 生成候选实验时必须引用真实证据：用户 ask 的主目标、代码分析中的入口/特征函数/可用参数、scene_metrics 的整体误差贡献（优先 `abs_error_sum`，其次才是 WAPE）、badcases 样本和字段映射；证据不足时输出“需要补充的证据”，不要硬编高级特征名。
- 候选实验必须包含 feature_name、field_sources、construction、code_locations、validation_metrics、expected_effect 和 risk；validation_metrics 的第一项必须是用户 ask 指定的主指标展示名，第二项可写源码映射后的内部指标。
- 候选实验必须对齐源码可执行链路：优先复用已有 CLI 参数、函数或 feature_cols 返回路径；不得只新增 `--enable-*` 空开关。若建议使用 CLI 参数，必须能在源码 argparse 中找到该参数，并能在非 parse_args 逻辑中看到消费证据。
- 对已有通用 rolling/lag 特征框架，优先建议扩展现有窗口或参数，例如把 `rolling_windows` 从短窗扩到 14/30，而不是发明一个源码不认识的新特征开关。
- 对已有 lifecycle、days_to_end、calibration 等能力，必须说明新增点是分桶、交叉、参数调整还是分组调整；不要把已有连续特征重复包装成“新特征”。
- 校准类建议只能使用预测时可得字段分组，禁止使用 underestimate/overestimate 这类事后误差标签作为在线特征或分组，避免泄漏。
- 不允许固定输出 festival_model_features、distribution_features 等场景特化候选，除非代码、字段、badcase 或用户 ask 明确支持这些方向。
- 对 high_target + underestimate 场景，优先考虑源码已支持的近期目标滚动窗口、动量、生命周期/结束窗口交叉和源码已有校准器分组调整；没有源码接入点时先建议定位接入链路。

# 输出格式

输出：

```markdown
# 优化建议

## 建议 1：<方向>

- 证据：<来自日志/代码/指标/badcase/异常的证据>
- 动作：<建议下一步动作>
- 验证：<如何验证建议是否有效>
```

单点任务可以输出 2-4 条简短建议；完整分析再输出分组建议文档。

# 易错点

- 日志 warning 只能支撑“需要排查”，不能直接支撑“就是原因”。
- badcase 聚集在某类样本时，应建议做分层复核，而不是直接改模型。
- 指标变差需要明确指标来源；没有指标时不要编造。
- 建议要能被实验人员执行，不要停留在“优化模型”这类空话。
- 建议中的动作必须能被 Agent2 转成代码或数据处理步骤，例如“扩展已有 `rolling_windows` 并确认 rolling_14/rolling_30 进入 feature_cols”，而不是“优化高误差场景”。
- 不要用硬编码关键词替代模型判断；模型需要读 ask、结构化证据和 skill 规则后形成建议重点。
- 扫描只发现缺失或模糊产物时，不要把“无法评测”写成实验失败。

# 运行资源

- 生成建议后执行 `scripts/validate_suggestions.py`；不要读取脚本正文，除非脚本失败需要调试。
- 自建 agent 框架可选使用 `scripts/write_suggestions.py` 基于结构化证据生成确定性建议草稿；LLM 可在此基础上润色，但不得新增证据。
- 写建议分类时读取 `references/suggestion-rules.md`。
- 只有调整触发覆盖时读取 `references/eval_cases.md`。

# 质量检查

最终输出前确认：

- 每条建议都有“证据、动作、验证”。
- Diagnostic 模式没有确定性效果结论。
- 没有新增报告上下文中不存在的证据。
- 没有基线相关内容。
- 建议范围与用户请求范围一致，没有把单点请求扩写成完整报告。
