---
name: forecast-trial-codegen
description: 当 Agent2 需要在 runs/<trial>/code/ 中修改预测实验 trial 副本、生成可执行特征代码、修复代码生成审计失败或提升 trial 代码生成成功率时使用。不用于评测指标计算、badcase 分析、业务源码直接修改或业务特化特征硬编码。
---

# Forecast Trial Codegen

硬规则：

1. 只修改 `runs/<trial>/code/` 中已复制的 trial 代码文件，路径只返回 basename。
2. 只实现 Agent1 优先级最高的一条 feature change，忽略其它候选建议。
3. 优先使用窄编辑：`add_feature_column_in_function`、`insert_before_return`、`insert_after_line`、`replace_lines`。
4. 只有窄编辑无法表达时才使用 `replace_function`，禁止重写整条训练流程。
5. 保留原函数签名、默认值、关键字调用兼容性和 return arity。
6. 使用源码已有参数名，如 `label_col`、`target_col`、`date_col`、`id_cols`、`group_cols`、`args`。
7. 做最小可执行修改；新增 helper 必须被已有 train/feature 路径调用。
8. 新特征必须落到真实特征列、helper 调用或 CLI 消费路径，不能只写 metadata、注释或日志。
9. 特征列或 helper 名称必须包含可审计 token，例如 `zero_history_flag`。
10. 新增目标历史 rolling/lag 特征必须 shift，不能泄漏当前行 label。
11. 不改 split、label、metric、model family、loss/objective、清洗逻辑、category maps、输出格式。
12. `train.py` 只在需要 wiring、默认参数、argparse、preset 或输出路径时修改。
13. 若只改依赖文件，必须确认 `train.py` 能 import/call 到修改路径。
14. 返回 YAML only，不要 Markdown、解释文本或 diff。
