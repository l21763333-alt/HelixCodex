# 产物命名规则

## 目录优先级

优先关注这些目录下的文件：

- `output`
- `outputs`
- `result`
- `results`
- `eval`
- `evaluation`
- `logs`
- `data`
- `configs`

## 文件名线索

prediction 候选：

- `prediction`
- `pred`
- `forecast`

actual 候选：

- `actual`
- `label`
- `truth`
- `sales`

metrics 候选：

- `metric`
- `metrics`
- `eval`
- `result`

日志候选：

- `train.log`
- `eval.log`
- `training.log`
- `pipeline.log`

## 模糊处理

如果同一类产物有多个候选且分数接近，标记为 ambiguous。不要让 LLM 凭文件名直觉选择。
