---
name: forecast-trial-codegen
description: 当 Agent2 需要在 runs/<trial>/agent2/code/ 中修改预测实验 trial 副本、生成可执行特征代码、修复代码生成审计失败或提升 trial 代码生成成功率时使用。不用于评测指标计算、badcase 分析、业务源码直接修改或业务特化特征硬编码。
---

# Forecast Trial Codegen

## Canonical Path

Only the following trial code directory is writable for model-code changes:

```text
runs/<trial>/agent2/code/
```

Do not create or modify:

```text
runs/<trial>/code/
```

If a path is requested in output YAML, use paths relative to `agent2/code/`.

## Hard Rules

1. Only modify files already copied under `runs/<trial>/agent2/code/`.
2. Generate or update `runs/<trial>/agent2/code/train.py` as the training entrypoint.
3. Generate `runs/<trial>/agent2/agent2_execution_plan.yaml` only after validation passes.
4. Implement only the highest-priority feature change from Agent1 unless explicitly instructed otherwise.
5. Prefer narrow edits: `add_feature_column_in_function`, `insert_before_return`, `insert_after_line`, or `replace_lines`.
6. Use `replace_function` only when narrow edits cannot express the change; do not rewrite the full training pipeline.
7. Preserve function signatures, default values, keyword compatibility, and return arity.
8. Use existing source argument names such as `label_col`, `target_col`, `date_col`, `id_cols`, `group_cols`, and `args`.
9. New helpers must be called by the real train/feature path; metadata-only, comments-only, and logs-only changes do not count.
10. New feature columns must have auditable names, such as `zero_history_flag`.
11. Any rolling or lag feature derived from targets must shift before using the current row label.
12. Do not change split logic, label definition, metric definition, model family, output schema, or category maps unless explicitly requested.
13. `train.py` may be changed for wiring, argparse, presets, or output paths, but it must remain importable and executable.
14. Run syntax validation with `python -m py_compile agent2/code/train.py` from the trial root.
15. Run a smoke import using `agent2/code/src` on `sys.path` before declaring success.
16. Return YAML only. Do not return Markdown, prose explanations, or raw diffs.

