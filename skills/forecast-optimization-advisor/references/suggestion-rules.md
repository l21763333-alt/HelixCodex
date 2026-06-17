# 建议生成规则

每条优化建议必须包含三部分：

```text
证据：<来自哪类结构化结果>
动作：<下一步要做什么>
验证：<如何判断动作是否有效>
```

允许使用的证据来源：

- 日志摘要
- 代码结构分析
- 整体指标
- 场景指标
- badcase 明细
- 异常摘要
- 缺失产物
- 模糊产物
- 源码中的 argparse 参数、特征函数和 feature_cols 路径

优先级：

- 场景建议先按 `abs_error_sum` 或整体误差贡献排序；只有贡献相近时再比较 WAPE。
- 特征实验优先复用源码已有参数/函数链路，例如 rolling window 参数、build_features、feature_cols 返回路径。
- 已存在 lifecycle、days_to_end 或 calibrator 能力时，建议必须写清新增的是分桶、交叉、参数调整或分组调整，不能重复包装已有特征。
- 校准分组只能使用预测时可得字段，不得使用 underestimate/overestimate 这类事后误差标签。

禁止：

- 没有直接证据就写确定性根因
- 暗示已经自动重新训练
- 暗示已经修改实验代码
- Diagnostic 模式下写上线判断或模型好坏判断
- 发明源码 argparse 不认识、也没有真实消费链路的 `--enable-*` 空开关
