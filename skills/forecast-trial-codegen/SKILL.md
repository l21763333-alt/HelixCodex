---
name: forecast-trial-codegen
description: 当 Agent2 需要在注入的 {trial_code_dir} 中修改预测实验 trial 副本、生成可执行特征代码、修复代码生成审计失败或提升 trial 代码生成成功率时使用。不用于评测指标计算、badcase 分析、业务源码直接修改或业务特化特征硬编码。
---

# Forecast Trial Codegen

## Canonical Path

Only the injected trial code directory is writable for model-code changes:

```text
{trial_code_dir}
```

In the current Codex Flow path contract this usually resolves to:

```text
runs/<trial>/candidate/code/
```

Do not write model-code changes to legacy trial-code directories, the original experiment directory, or the Git worktree.

If a path is requested in output YAML, use paths relative to `{trial_code_dir}` unless the prompt explicitly asks for the absolute injected path.

## Model Contract

Use the injected `model_contract` as the source of truth for:

- copied source layout and `copy_include` / `copy_exclude`
- `entrypoint_candidates`
- `default_train_command`
- `requirements_paths`, which may be empty or contain multiple files
- `output_contract`, including prediction path, split column/filter, metric columns, and accepted candidate columns

Do not assume a fixed entrypoint, fixed package name, fixed requirements file, fixed CLI flag, or fixed output table.

## Hard Rules

1. Only modify files under `{trial_code_dir}`.
2. Generate or update `{trial_code_dir}/train.py` as the candidate training wrapper when no existing entrypoint already satisfies the execution plan.
3. Generate `agent2/agent2_execution_plan.yaml` only after validation passes.
4. Implement only the highest-priority feature change from Agent1 unless explicitly instructed otherwise.
5. Prefer narrow edits: `add_feature_column_in_function`, `insert_before_return`, `insert_after_line`, or `replace_lines`.
6. Use `replace_function` only when narrow edits cannot express the change; do not rewrite the full training pipeline.
7. Preserve function signatures, default values, keyword compatibility, and return arity.
8. Use existing source argument names such as `label_col`, `target_col`, `date_col`, `id_cols`, `group_cols`, and `args` when they exist; do not invent business-specific names.
9. New helpers must be called by the real train/feature path; metadata-only, comments-only, and logs-only changes do not count.
10. New feature columns must have auditable names, such as `zero_history_flag`.
11. Any rolling or lag feature derived from targets must shift before using the current row label.
12. Do not change split logic, label definition, metric definition, model family, output schema, or category maps unless explicitly requested.
13. `train.py` may be changed for wiring, argparse, presets, or output paths, but it must remain importable and executable.
14. Run syntax validation with `python -m py_compile {trial_code_dir}/train.py` from the trial root.
15. Run a smoke check using the model contract's `entrypoint_candidates`; if none import cleanly, validate that `train.py` can execute a harmless help/import path.
16. Return YAML only. Do not return Markdown, prose explanations, or raw diffs.
