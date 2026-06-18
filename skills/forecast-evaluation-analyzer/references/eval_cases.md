# 评测用例

## 应触发

- “prediction 和 actual 都在，计算 WAPE/MAPE/Bias”
- “做完整预测评测”
- “按周末、低目标值、高目标值拆解误差”

## 不应触发

- “没有 actual，先看日志”
- “帮我训练模型”
- “解释 WAPE 是什么，但没有实验数据”

## 边界场景

- “只找到 metrics.csv”：进入诊断或已有指标摘要，不重新计算。
- “prediction 有多个”：先由产物发现标记 ambiguous，不进入 Full 模式。
- “actual 有 0”：指标脚本处理，LLM 不手算 MAPE。
