---
name: forecast-experiment-scanner
description: 当需要从预测实验目录发现代码、配置、日志、prediction、actual 或 metrics 产物时使用。典型触发包括“扫描这个实验目录”“自动找预测结果和真实值”“看看实验目录里有哪些产物”。不用于通用文件搜索、普通项目目录介绍、或不涉及预测评测产物发现的任务。
---

# 目的

把预测实验目录中的相关文件分成代码、配置、日志、数据、prediction 候选、actual 候选和 metrics 候选。这个 skill 是通用 forecast 扫描能力，不绑定任何具体业务场景或某个固定文件名。

# 输入

可接受输入：
- 实验目录路径
- 用户 `ask`
- 可选人工覆盖路径：`--log`、`--prediction`、`--actual`
- `scan_experiment.py` 的扫描结果

仅在以下情况追问：
- 实验目录不存在
- 用户提供的人工覆盖路径不存在
- 用户要求读取本地实验目录以外的敏感路径且没有明确授权

# 工作流

1. 先执行 `scripts/scan_experiment.py` 做 focused scan。
2. 扫描器不得无差别递归全目录；先根据目录名选择可能相关的目录，例如 `src/`、`scripts/`、`data/`、`outputs/`、`results/`、`logs/`、`config/`。
3. 对名称不明显的目录，只读取少量候选文件的头部几行，通过注释、表头或开头文本判断是否包含 forecast / predict / actual / metric / train / eval 等线索。
4. 如果用户传入的是过窄子目录，例如 `src/`，且未找到 prediction、actual、metrics 或日志等关键产物，则回退到上一级目录重新做 focused scan；最多只做有限层级 fallback。
5. 识别代码、配置、日志、数据、prediction、actual、metrics 和通用业务产物候选。
6. 执行 `scripts/discover_artifacts.py`，根据人工覆盖、目录名、文件名、mtime 和任务关键词选择文件。
7. 如果 prediction 或 actual 有多个同等候选，标记 `ambiguous`，不要让 LLM 自己选。
8. 输出 `artifact_summary.json`，供模式选择和报告使用。
9. 如果找到 prediction 但找不到日志，报告最后必须说明日志证据缺失，不能写成模型能力不可用或训练无异常。

# 规则

- 文件选择必须由 deterministic tool 完成；LLM 只基于扫描摘要和文件头部线索做解释与下一步判断。
- 不写死业务类型，不得把通用扫描逻辑绑定到某个具体业务场景、产物命名或业务线。
- 训练入口、源码根目录和 requirements 位置可能由 model contract 配置，不得假设固定为 `src/` 或单个 `requirements.txt`。
- 可以保留历史兼容字段，但报告主线必须使用通用字段，例如 `domain_artifact_files`、`candidate_prediction_files`、`actual_files`、`log_files`。
- 多候选无法可靠判断时，标记 ambiguous。
- 找不到 prediction 或 actual 不报错，后续进入 Diagnostic 模式。
- 找不到日志不报错，但报告必须说明日志证据不可用。
- 找不到代码不报错，报告中说明代码分析不可用。
- 不要根据用户 ask 直接拼出不存在的路径。
- 扫描阶段建议只能基于已发现、缺失或 ambiguous 的产物状态，不评价模型效果。

# 输出格式

扫描输出应至少包含：

```json
{
  "requested_dir": "用户传入目录",
  "experiment_dir": "实际扫描目录",
  "fallback_used": false,
  "scan_strategy": "focused_dir_and_header_scan",
  "scanned_dirs": [],
  "code_files": [],
  "config_files": [],
  "log_files": [],
  "data_files": [],
  "candidate_prediction_files": [],
  "actual_files": [],
  "metrics_files": [],
  "domain_artifact_files": [],
  "possible_entrypoints": [],
  "warnings": []
}
```

artifact discovery 输出：

```json
{
  "prediction_path": null,
  "actual_path": null,
  "log_path": null,
  "metrics_paths": [],
  "confidence": {},
  "missing_artifacts": [],
  "ambiguous_artifacts": []
}
```

# 易错点

- 不要递归扫描 `.python_packages`、`.venv`、`node_modules`、`archive`、`.comboscope_backups`、缓存目录或历史备份目录。
- `prediction_old.csv` 和 `prediction_new.csv` 同时存在时，不要只因名字像就随机选。
- `metrics.csv` 不是 prediction，也不是 actual。
- `label.csv`、`truth.csv`、`actual.csv`、`truth.csv`、`label.csv` 都可能是真实值候选，但需要 discovery 规则确认。
- `eval_result.csv` 可能是指标文件，也可能是预测结果；不确定就标记 ambiguous。
- 文件名不明显时，优先看文件头部几行注释或 CSV 表头，而不是全量读取文件。
- 如果当前目录匹配不上，先回退上一级实验目录；不要扩大到任意上层大目录。

# 运行资源

- 执行 `scripts/scan_experiment.py` 做目录分类；不要读取脚本正文，除非脚本失败需要调试。
- 执行 `scripts/discover_artifacts.py` 做最终产物选择。
- 只有需要调整命名规则时读取 `references/artifact-patterns.md`。
- 只有调整触发覆盖时读取 `references/eval_cases.md`。

# 质量检查

最终输出前确认：

- 没有由 LLM 直接决定模糊文件。
- ambiguous 文件写入 `ambiguous_artifacts`。
- 缺失文件写入 `missing_artifacts` 或 scan `warnings`。
- `artifact_summary.json` 中的路径真实存在，或为 `null`。
- 找到 prediction 但缺少日志时，报告中明确说明日志证据缺失。
- 如输出建议，建议已引用 `artifact_summary.json` 或 scan `warnings` 中的 missing / ambiguous 证据。
