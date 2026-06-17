from __future__ import annotations

import json
import importlib.util
import re
from pathlib import Path
from typing import Any


DIAGNOSTIC_NOTICE = "由于缺少 prediction 或 actual，本次无法重新计算 WAPE/MAPE/Bias。当前结论仅基于训练日志、代码和已有产物分析。"

METRIC_LABELS = {
    "wape": "WAPE（主指标，整体加权绝对误差，越低越好）",
    "mape": "MAPE（平均相对误差，越低越好；小目标值样本会放大该指标）",
    "bias": "Bias（整体偏差，负数代表整体低估，正数代表整体高估，越接近 0 越好）",
    "mae": "MAE（平均绝对误差，单位与预测目标一致，越低越好）",
    "rmse": "RMSE（均方根误差，对大误差更敏感，越低越好）",
    "rows": "样本数（参与评测的预测样本数量）",
    "sample_count": "样本数（参与评测的预测样本数量）",
}

SCENE_LABELS = {
    "weekday": "工作日",
    "weekend": "周末",
    "high_target": "高目标值样本",
    "normal_target": "中等目标值样本",
    "low_target": "低目标值样本",
    "high_sales": "高目标值样本（旧标签兼容）",
    "normal_sales": "中等目标值样本（旧标签兼容）",
    "low_sales": "低目标值样本（旧标签兼容）",
    "underestimate": "低估",
    "overestimate": "高估",
    "exact": "预测等于真实",
    "holiday": "节假日",
    "not_holiday": "非节假日",
}

BADCASE_LABELS = {
    "top_abs_error": "绝对误差最大的样本",
    "top_ape": "相对误差最大的样本",
    "top_underestimate": "低估最严重的样本",
    "top_overestimate": "高估最严重的样本",
    "extreme_underestimate": "极端低估样本",
    "extreme_overestimate": "极端高估样本",
    "consecutive_underestimate": "连续低估样本",
    "consecutive_overestimate": "连续高估样本",
}

ANOMALY_LABELS = {
    "systematic_bias": "整体存在系统性偏差",
    "high_wape": "整体 WAPE 偏高",
    "scene_high_error": "部分细分场景误差偏高",
}


def build_suggestions(context: dict[str, Any]) -> list[str]:
    suggestions: list[str] = []
    run_status = context.get("run_status") or {}
    if run_status.get("train_returncode") == "agent2_code_generation_failed" or run_status.get("agent2_code_generation_success") is False:
        record = run_status.get("agent2_code_modification_path") or "code/agent2_code_modification.yaml"
        suggestions.append(f"- 本轮实验结论：Agent2 代码生成失败，未产生通过编译和特征应用审计的 trial 代码；已跳过训练。修改记录：{record}。")
    elif run_status.get("feature_application_success") is False:
        audit_path = run_status.get("feature_application_audit_path") or "code/agent2_feature_application_audit.yaml"
        suggestions.append(f"- 本轮实验结论：特征应用审计未通过，Agent2 没有找到本轮特征被真实训练代码消费的证据；已跳过训练。审计文件：{audit_path}。")
    advisor_items = _advisor_suggestions(context)
    concrete_feature_items = [item for item in advisor_items if _is_concrete_feature_item(item)]
    if advisor_items:
        for item in advisor_items:
            if concrete_feature_items and _is_generic_diagnostic_item(item):
                continue
            suggestions.append(_clean_generated_text(_format_advisor_suggestion(item), context))
    comparison = context.get("metric_comparison") or {}
    if comparison and run_status.get("feature_application_success") is not False:
        decision = comparison.get("decision")
        wape_delta = comparison.get("wape_delta")
        bias_delta = comparison.get("bias_delta")
        primary_label = _primary_label(context)
        primary_delta = _primary_delta(context)
        if decision == "keep":
            suggestions.append(f"- 本轮实验结论：满足 `{primary_label}` 的 keep 规则，主目标变化 {primary_delta}，可以保留该 trial 的特征方向。")
        elif decision == "rollback":
            suggestions.append(
                f"- 本轮实验结论：未满足 `{primary_label}` 的 keep 规则，主目标变化 {primary_delta}，WAPE 变化 {wape_delta}，Bias 绝对值变化 {bias_delta}，不建议保留该特征改动。"
            )
    artifact = context.get("artifact_summary", {})
    scan = context.get("scan_result", {})
    missing = artifact.get("missing_artifacts", [])
    ambiguous = artifact.get("ambiguous_artifacts", {})
    scan_warnings = scan.get("warnings", [])
    if missing:
        suggestions.append(f"- 证据：自动发现阶段缺少 {missing}。说明：这不是模型能力不可用，而是没有在扫描结果中找到对应 artifact；建议确认 experiment 参数是否指向真实实验目录，或把日志/指标输出到约定路径。")
    if "log file not found" in scan_warnings and not run_status.get("train_log_path"):
        suggestions.append("- 证据：扫描阶段未找到训练或评测日志。建议：本轮报告只能基于已有 prediction/actual/代码产物分析，后续请补充 train.log 或 eval.log 以复核训练过程。")
    elif "log file not found" in scan_warnings and run_status.get("train_log_path"):
        suggestions.append(f"- 证据：原 baseline 扫描阶段未找到训练日志；但本轮 Agent2 trial 已生成训练日志，可复核路径：{run_status.get('train_log_path')}。")
    if scan.get("fallback_used"):
        suggestions.append("- 证据：用户传入目录过窄，扫描器已回退到上一级实验目录。建议：下次直接把 --experiment 指向完整实验目录。")
    if ambiguous:
        suggestions.append(f"- 证据：存在模糊产物 {list(ambiguous)}。建议：重命名输出文件，或通过 CLI 覆盖参数显式指定路径，避免 Agent 选错评测对象。")
    if not suggestions:
        suggestions.append("- 建议：当前证据不足以提出具体特征实验；请先补齐 scene_metrics.csv、badcases.csv 或训练日志，再生成可执行特征清单。")
    return suggestions


def write_template_report(context: dict[str, Any], report_path: str | Path, suggestions_path: str | Path | None = None) -> None:
    report = build_template_report(context)
    Path(report_path).write_text(report, encoding="utf-8")
    if suggestions_path:
        Path(suggestions_path).write_text("\n".join(build_suggestions(context)) + "\n", encoding="utf-8")


def write_report(context: dict[str, Any], report_path: str | Path, suggestions_path: str | Path | None = None) -> None:
    raise RuntimeError(
        "write_report.py is a template test helper only. Runtime forecast_report.md must be generated by Agent1 LLM."
    )


def build_template_report(context: dict[str, Any]) -> str:
    mode = context["mode"]
    scan = context.get("scan_result", {})
    artifact = context.get("artifact_summary", {})
    code = context.get("code_analysis", {})
    log = context.get("log_summary", {})
    plan = context.get("experiment_plan", {})
    run_status = context.get("run_status", {})
    comparison = context.get("metric_comparison", {})
    suggestions = build_suggestions(context)

    lines = [
        "# 预测实验评测报告",
        "",
        "## 1. 一句话结论",
        f"- {_one_sentence_conclusion(context)}",
        f"- 主要问题定位：{_primary_problem_text(context)}",
    ]
    if mode == "diagnostic":
        lines.extend(["", f"- {DIAGNOSTIC_NOTICE}"])

    lines.extend(
        [
            "",
            "## 2. 任务理解",
            f"- 用户问题：{context.get('task', {}).get('ask', '')}",
            "- 本报告目标：定位主要误差模式，区分 baseline 原始指标与本轮 trial 指标，说明本轮特征实验改了什么、是否有效，以及下一步应该怎么验证。",
            "",
            "## 3. 实验目录扫描结果",
            f"- 扫描策略：{scan.get('scan_strategy', 'focused_dir_and_header_scan')}（优先根据目录名和文件头部线索选取相关文件，不做全目录无差别扫描）",
            f"- 用户传入目录：{scan.get('requested_dir', scan.get('experiment_dir'))}",
            f"- 实际扫描目录：{scan.get('experiment_dir')}",
            f"- 是否回退上一级目录：{scan.get('fallback_used', False)}",
            f"- 重点扫描目录：{_format_list(scan.get('scanned_dirs', []), 12)}",
            f"- 扫描到代码文件：{len(scan.get('code_files', []))} 个",
            f"- 扫描到配置文件：{len(scan.get('config_files', []))} 个",
            f"- 扫描到日志文件：{len(scan.get('log_files', []))} 个",
            f"- 扫描到数据文件：{len(scan.get('data_files', []))} 个",
            f"- prediction 候选：{_format_list(scan.get('candidate_prediction_files', []), 8)}",
            f"- actual 候选：{_format_list(scan.get('actual_files', []), 8)}",
            f"- metrics 候选：{_format_list(scan.get('metrics_files', []), 8)}",
            f"- 业务产物候选：{_format_list(scan.get('domain_artifact_files', []), 8)}",
            f"- 训练/评估入口候选：{_format_list(scan.get('possible_entrypoints', []), 8)}",
            f"- 扫描文件样例：{_scan_samples(scan)}",
            f"- 扫描缺失提醒：{_format_list(scan.get('warnings', []), 8)}",
            "- 说明：如果 prediction 已找到但日志缺失，报告结论会明确标注日志证据不足；如果文件数量明显偏少，通常说明 `--experiment` 指向了子目录。",
            "",
            "## 4. 日志解析结果",
        ]
    )

    if not log.get("available"):
        if run_status.get("train_log_path"):
            lines.append("- 原 baseline 扫描阶段未找到训练日志，因此日志解析摘要不可用；但本轮 Agent2 trial 已生成训练日志。")
            lines.append(f"- Agent2 trial 训练日志路径：{run_status.get('train_log_path')}")
            failure_summary = _train_failure_summary(run_status)
            if failure_summary:
                lines.append(f"- trial 训练失败关键错误：{failure_summary}")
            trial_log_metrics = _trial_log_metrics(run_status)
            if trial_log_metrics:
                lines.append(f"- trial 日志关键指标：{trial_log_metrics}")
        else:
            lines.append("- 日志解析未执行或未找到日志文件；这不是模型能力不可用，而是本次 report_context 中没有可解析的 train.log 路径。")
    else:
        lines.extend(
            [
                f"- 运行状态：{log.get('status')}",
                f"- error 片段：{_format_list(log.get('errors', []), 3)}",
                f"- warning 片段：{_format_list(log.get('warnings', []), 3)}",
                f"- 日志中的指标：{log.get('metric_values', {})}",
            ]
        )

    lines.extend(
        [
            "",
            "## 5. 代码结构与实验执行说明",
            f"- 原实验入口：{plan.get('source_entrypoint') or _format_list(code.get('entrypoints', []), 5)}",
            f"- trial 训练副本：{run_status.get('generated_train_path') or plan.get('generated_train_path')}",
            f"- 训练数据/配置清单：{run_status.get('data_manifest_path', '未生成')}",
            f"- 训练结果目录：{run_status.get('real_output_dir', '未生成')}",
            f"- 本轮特征改动：{_format_changes(plan.get('changes', []))}",
            f"- Agent2 实际修改文件：{_agent2_modified_files_text(context)}",
            f"- 特征应用审计：{_feature_application_audit_text(context)}",
            f"- 执行方式：{_execution_method_text(plan, run_status)}",
            f"- 评估入口/指标函数：评估由 ComboScope skills 读取标准化 prediction/actual 后确定性计算；指标函数线索：{_format_list(code.get('metric_functions', []), 8)}",
            "",
            "## 6. 评测数据可用性",
            f"- prediction 路径：{artifact.get('prediction_path')}",
            f"- actual 路径：{artifact.get('actual_path')}",
            f"- Agent2 标准化 prediction：{run_status.get('prediction_path', '未生成')}",
            f"- Agent2 标准化 actual：{run_status.get('actual_path', '未生成')}",
            f"- 缺失产物：{artifact.get('missing_artifacts', [])}",
            f"- 模糊产物：{artifact.get('ambiguous_artifacts', {})}",
            "",
            "## 7. 整体指标",
        ]
    )

    if mode == "full":
        metric_note = _metric_definition_note(context)
        if metric_note:
            lines.append(f"- {metric_note}")
        comparison_lines = _metric_comparison_lines(context)
        if comparison_lines:
            lines.extend(comparison_lines)
            lines.extend(_metric_detail_lines("baseline 原始评测细项", context.get("metrics")))
            if _has_valid_trial_metrics(context):
                lines.extend(_metric_detail_lines("trial 修改后评测细项", context.get("new_metrics")))
        else:
            for row in context.get("metrics", []):
                metric = str(row.get("metric", ""))
                label = METRIC_LABELS.get(metric.lower(), metric)
                lines.append(f"- {label}：{row.get('value')}")
    else:
        lines.append("- Diagnostic 模式不重新计算指标。")

    lines.extend(["", "## 8. 细粒度效果拆解"])
    if comparison:
        lines.append("- 注意：本节场景拆解来自 baseline 原始评测产物，用于解释本轮实验设计依据；它不是 trial 修改后的场景指标。")
    lines.append("- 场景标签说明：weekday/weekend 表示工作日/周末；high_target/normal_target/low_target 表示预测目标值分层；underestimate/overestimate 表示低估/高估。")
    if mode == "full" and context.get("scene_metrics"):
        for row in context.get("scene_metrics", [])[:10]:
            scene = str(row.get("scene", ""))
            lines.append(
                f"- {_translate_scene(scene)}：样本数={row.get('rows')}, WAPE={row.get('wape')}, Bias={row.get('bias')}"
            )
    elif mode == "full":
        lines.append("- 未生成可用的场景指标。")
    else:
        lines.append("- Diagnostic 模式不生成细粒度效果拆解。")

    lines.extend(["", "## 9. badcase 定位"])
    if comparison:
        lines.append("- 注意：本节 badcase 来自 baseline 原始评测产物，用于说明改动假设来源；它不是 trial 修改后的 top badcase。")
    lines.append("- badcase 是误差最大的样本，用来定位“模型最容易错在哪里”，不是单独一条样本就代表全局根因。")
    if mode == "full":
        for row in context.get("badcases", [])[:10]:
            kind = BADCASE_LABELS.get(str(row.get("badcase_type")), str(row.get("badcase_type")))
            lines.append(
                f"- {kind}：真实值={row.get('actual')}, 预测值={row.get('prediction')}, 绝对误差={row.get('abs_error')}"
            )
    else:
        lines.append("- Diagnostic 模式不生成 badcase 明细。")

    lines.extend(["", "## 10. 优化建议"])
    lines.extend(suggestions)
    lines.extend(["", "## 11. 下一步动作"])
    if comparison:
        primary_label = _primary_label(context)
        lines.append(f"- 中文结论：本轮实验决策为 `{comparison.get('decision')}`，本轮以 `{_display_primary_label(primary_label, context)}` 作为主决策口径。")
    if mode == "diagnostic":
        lines.append("- 补充或消歧 prediction 和 actual 文件后，重新运行完整评测。")
    else:
        lines.append(f"- 下一轮模型迭代前，先执行第 10 节列出的具体特征实验，并用 `{_display_primary_label(_brief_label(context), context)}` 和 signed Bias 辅助观察高估/低估方向。")

    return "\n".join(lines) + "\n"


def _advisor_suggestions(context: dict[str, Any]) -> list[dict[str, str]]:
    advisor_path = Path(__file__).resolve().parents[2] / "forecast-optimization-advisor" / "scripts" / "write_suggestions.py"
    if not advisor_path.exists():
        return []
    spec = importlib.util.spec_from_file_location("comboscope_forecast_optimization_advisor", advisor_path)
    if not spec or not spec.loader:
        return []
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    items = module.build_suggestions(context)
    return [item for item in items if isinstance(item, dict)]


def _format_advisor_suggestion(item: dict[str, str]) -> str:
    title = item.get("title", "未命名建议")
    evidence = item.get("evidence", "未提供证据")
    action = item.get("action", "未提供动作")
    validation = item.get("validation", "未提供验证方式")
    return f"- 建议：{title}。证据：{evidence}。动作：{action}。验证：{validation}"


def _is_concrete_feature_item(item: dict[str, str]) -> bool:
    title = item.get("title", "")
    action = item.get("action", "")
    concrete_markers = [
        "构建",
        "验证 extend_rolling_windows_14_30",
        "复核 source_aligned_group_calibration",
        "_rolling_",
        "_trend_",
        "_bucket",
        "_calibration_",
        "rolling_windows",
    ]
    return any(marker in title or marker in action for marker in concrete_markers)


def _is_generic_diagnostic_item(item: dict[str, str]) -> bool:
    title = item.get("title", "")
    generic_titles = {
        "拆解高误差来源",
        "复核系统性偏差",
        "按整体误差贡献复核场景",
        "抽样核查 badcase",
        "处理异常摘要信号",
    }
    return title in generic_titles


def _one_sentence_conclusion(context: dict[str, Any]) -> str:
    scenes = _priority_scenes(context.get("scene_metrics") or [])
    metrics = {str(row.get("metric", "")).lower(): row.get("value") for row in context.get("metrics", [])}
    worst_scene = scenes[0].get("scene") if scenes else "主要误差场景未明确"
    comparison = context.get("metric_comparison") or {}
    run_status = context.get("run_status") or {}
    if run_status.get("feature_application_success") is False:
        return f"主要问题集中在 `{worst_scene}`；本轮 Agent2 特征应用审计未通过，实验决策为 `rollback`。"
    if comparison:
        decision = comparison.get("decision")
        primary_label = _primary_label(context)
        return f"主要问题集中在 `{worst_scene}`；本轮以 `{_display_primary_label(primary_label, context)}` 为主决策口径，实验决策为 `{decision}`。"
    if metrics:
        return f"主要问题集中在 `{worst_scene}`；当前 WAPE={metrics.get('wape', 'n/a')}，Bias={metrics.get('bias', 'n/a')}，建议优先处理对应低估/高估场景。"
    return "当前证据不足以重新计算完整指标，建议先补齐 prediction/actual/log 等关键产物。"


def _primary_problem_text(context: dict[str, Any]) -> str:
    scenes = _priority_scenes(context.get("scene_metrics") or [])
    if scenes:
        scene = scenes[0]
        return f"当前优先关注 `{scene.get('scene')}`，该场景样本数={scene.get('rows')}，abs_error_sum={scene.get('abs_error_sum')}，WAPE={scene.get('wape')}，Bias={scene.get('bias')}。"
    anomalies = context.get("anomaly_summary", {}).get("anomalies", [])
    if anomalies:
        labels = [ANOMALY_LABELS.get(item.get("type"), str(item.get("type"))) for item in anomalies]
        return "当前异常包括：" + "、".join(labels)
    return "当前未形成稳定的场景级问题定位，需要补充更多评测产物。"


def _brief_label(context: dict[str, Any]) -> str:
    definition = _metric_definition(context)
    comparison = context.get("metric_comparison") if isinstance(context.get("metric_comparison"), dict) else {}
    if definition.get("objective_label") or definition.get("decision_metric"):
        return str(definition.get("objective_label") or definition.get("decision_metric"))
    if comparison.get("primary_metric_label") or comparison.get("primary_metric"):
        return str(comparison.get("primary_metric_label") or comparison.get("primary_metric"))
    if any(key in comparison for key in ("old_wape", "new_wape", "wape_delta")):
        return "WAPE"
    if any(key in comparison for key in ("old_bias", "new_bias", "bias_delta")):
        return "signed Bias"
    return "WAPE"


def _primary_label(context: dict[str, Any]) -> str:
    comparison = context.get("metric_comparison") if isinstance(context.get("metric_comparison"), dict) else {}
    return str(comparison.get("primary_metric_label") or comparison.get("primary_metric") or _brief_label(context))


def _primary_delta(context: dict[str, Any]) -> Any:
    comparison = context.get("metric_comparison") if isinstance(context.get("metric_comparison"), dict) else {}
    if comparison.get("primary_delta") is not None:
        return comparison.get("primary_delta")
    if _primary_label(context).lower() == "wape":
        return comparison.get("wape_delta")
    return None


def _metric_comparison_lines(context: dict[str, Any]) -> list[str]:
    comparison = context.get("metric_comparison") if isinstance(context.get("metric_comparison"), dict) else {}
    if not comparison:
        return []
    run_status = context.get("run_status") if isinstance(context.get("run_status"), dict) else {}
    if not _has_valid_trial_metrics(context):
        lines = [
            "- 执行状态与可用指标：本轮训练或评测失败，未产生有效 trial 指标。",
            f"- 本轮决策：`{comparison.get('decision')}`；原因：{_decision_reason_text(str(comparison.get('reason') or ''))}。",
            f"- 训练是否成功：{run_status.get('train_success')}；评测是否成功：{run_status.get('eval_success')}。",
        ]
        if run_status.get("train_log_path"):
            lines.append(f"- 训练日志路径：{run_status.get('train_log_path')}")
        failure_summary = _train_failure_summary(run_status)
        if failure_summary:
            lines.append(f"- 训练日志关键错误：{failure_summary}")
        lines.append("- 注意：new_metrics 若与 baseline 指标相同，是失败后的 carry-forward/fallback，不代表 trial 修改后指标持平。")
        return lines
    old_metrics = _metrics_map(context.get("metrics"))
    new_metrics = _metrics_map(context.get("new_metrics"))
    old_wape = comparison.get("old_wape", old_metrics.get("wape"))
    new_wape = comparison.get("new_wape", new_metrics.get("wape"))
    old_bias = comparison.get("old_bias", old_metrics.get("bias"))
    new_bias = comparison.get("new_bias", new_metrics.get("bias"))
    old_rows = _first_not_none(old_metrics.get("rows"), old_metrics.get("sample_count"))
    new_rows = _first_not_none(new_metrics.get("rows"), new_metrics.get("sample_count"))

    lines = [
        f"- 本轮对比对象：baseline 原实验 vs {_trial_name(context.get('run_status') or {})} 修改后实验。",
        f"- 本轮决策：`{comparison.get('decision')}`；原因：{_decision_reason_text(str(comparison.get('reason') or ''))}。",
        "",
        "| 指标 | baseline 原实验 | trial 修改后 | 变化 | 说明 |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    rows = [
        ("WAPE", old_wape, new_wape, comparison.get("wape_delta"), _wape_change_text(comparison.get("wape_delta"))),
        ("Bias", old_bias, new_bias, comparison.get("bias_delta"), _bias_change_text(old_bias, new_bias, comparison.get("bias_delta"))),
        ("MAPE", old_metrics.get("mape"), new_metrics.get("mape"), _numeric_delta(old_metrics.get("mape"), new_metrics.get("mape")), "越低越好"),
        ("MAE", old_metrics.get("mae"), new_metrics.get("mae"), _numeric_delta(old_metrics.get("mae"), new_metrics.get("mae")), "越低越好"),
        ("RMSE", old_metrics.get("rmse"), new_metrics.get("rmse"), _numeric_delta(old_metrics.get("rmse"), new_metrics.get("rmse")), "越低越好"),
        ("样本数", old_rows, new_rows, _numeric_delta(old_rows, new_rows), "样本量口径需一致才便于严格归因"),
    ]
    for name, old_value, new_value, delta, note in rows:
        if old_value is None and new_value is None:
            continue
        lines.append(f"| {name} | {_fmt(old_value)} | {_fmt(new_value)} | {_delta_cell(delta)} | {note} |")
    if old_rows is not None and new_rows is not None and _numeric_delta(old_rows, new_rows) != 0:
        lines.append(f"- 口径风险：baseline 与 trial 参与评测样本数不一致，{_fmt(old_rows)} -> {_fmt(new_rows)}。")
    return lines


def _has_valid_trial_metrics(context: dict[str, Any]) -> bool:
    run_status = context.get("run_status") if isinstance(context.get("run_status"), dict) else {}
    if run_status.get("train_success") is False or run_status.get("eval_success") is False:
        return False
    return bool(context.get("new_metrics"))


def _metrics_map(rows: Any) -> dict[str, Any]:
    if isinstance(rows, dict):
        return {str(key).lower(): value for key, value in rows.items()}
    if isinstance(rows, list):
        mapped: dict[str, Any] = {}
        for row in rows:
            if isinstance(row, dict) and row.get("metric") is not None:
                mapped[str(row.get("metric")).lower()] = row.get("value")
        return mapped
    return {}


def _metric_detail_lines(title: str, rows: Any) -> list[str]:
    metrics = _metrics_map(rows)
    if not metrics:
        return []
    lines = [f"- {title}："]
    for metric in ("wape", "mape", "bias", "mae", "rmse", "rows", "sample_count"):
        if metric not in metrics:
            continue
        label = METRIC_LABELS.get(metric, metric)
        lines.append(f"  - {label}：{metrics.get(metric)}")
    return lines


def _priority_scenes(scene_metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        scene_metrics,
        key=lambda row: (
            _to_float(row.get("abs_error_sum")) if _to_float(row.get("abs_error_sum")) is not None else -1.0,
            _to_float(row.get("wape")) if _to_float(row.get("wape")) is not None else -1.0,
        ),
        reverse=True,
    )


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _numeric_delta(old_value: Any, new_value: Any) -> float | None:
    old_float = _to_float(old_value)
    new_float = _to_float(new_value)
    if old_float is None or new_float is None:
        return None
    return new_float - old_float


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    number = _to_float(value)
    if number is not None:
        return f"{number:.6g}"
    return str(value)


def _delta_cell(value: Any) -> str:
    number = _to_float(value)
    if number is None:
        return "n/a"
    if number > 0:
        return f"+{_fmt(number)}"
    return _fmt(number)


def _wape_change_text(delta: Any) -> str:
    number = _to_float(delta)
    if number is None:
        return "WAPE 变化不可判断"
    if number > 0:
        return "越低越好的 WAPE 升高，效果变差"
    if number < 0:
        return "越低越好的 WAPE 下降，效果改善"
    return "WAPE 持平"


def _bias_change_text(old_bias: Any, new_bias: Any, abs_delta: Any) -> str:
    return f"方向：{_bias_direction(old_bias)} -> {_bias_direction(new_bias)}；绝对 Bias 变化 {_delta_cell(abs_delta)}"


def _bias_direction(value: Any) -> str:
    number = _to_float(value)
    if number is None:
        return "n/a"
    if number < 0:
        return "整体低估"
    if number > 0:
        return "整体高估"
    return "无明显整体偏差"


def _trial_name(run_status: dict[str, Any]) -> str:
    for key in ("generated_train_path", "real_output_dir", "train_log_path"):
        value = run_status.get(key)
        if not value:
            continue
        path = Path(str(value))
        for part in path.parts:
            if part.startswith("baseline_trial_") or part.startswith("trial_"):
                return part
    return "trial"


def _decision_reason_text(reason: str) -> str:
    translations = {
        "wape improved enough and bias stayed within threshold": "WAPE 改善达到 keep 阈值，且 Bias 未超过允许恶化范围",
        "train or evaluation failed": "训练或评测失败",
        "wape improvement is below threshold": "WAPE 改善幅度未达到 keep 阈值",
        "bias regression exceeds threshold": "Bias 恶化超过允许阈值",
        "agent2 code generation failed": "Agent2 代码生成失败，未进入训练",
        "feature change was not applied": "本轮特征改动未被真实训练代码消费",
    }
    return translations.get(reason, reason or "未记录")


def _trial_log_metrics(run_status: dict[str, Any]) -> str:
    path = Path(str(run_status.get("train_log_path") or ""))
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    snippets: list[str] = []
    patterns = [
        r"(?:wape|mape|bias|mae|rmse)\s*[:=]\s*[-+]?\d+(?:\.\d+)?",
        r"metrics?\s*[:=]\s*\{[^}]+\}",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            snippets.append(match.group(0))
    return "；".join(snippets)


def _train_failure_summary(run_status: dict[str, Any]) -> str:
    if run_status.get("train_success") is not False and run_status.get("eval_success") is not False:
        return ""
    explicit = run_status.get("train_failure_summary") or run_status.get("error")
    if explicit:
        return str(explicit)
    path = Path(str(run_status.get("train_log_path") or ""))
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    exception_prefixes = (
        "TypeError:",
        "ValueError:",
        "KeyError:",
        "FileNotFoundError:",
        "ModuleNotFoundError:",
        "ImportError:",
        "RuntimeError:",
        "AttributeError:",
        "NameError:",
        "SyntaxError:",
    )
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith(exception_prefixes):
            return stripped
    return ""


def _clean_generated_text(text: str, context: dict[str, Any]) -> str:
    primary = _display_primary_label(_brief_label(context), context)
    return (
        text.replace("用户指定优化目标", primary)
        .replace("用户 ask 指定的主指标", primary)
        .replace("主目标变化 None", f"主目标变化 {_primary_delta(context)}")
    )


def _metric_definition_note(context: dict[str, Any]) -> str:
    definition = _metric_definition(context)
    label = str(definition.get("objective_label") or context.get("metric_comparison", {}).get("primary_metric_label") or "")
    decision_metric = str(definition.get("decision_metric") or context.get("metric_comparison", {}).get("decision_metric") or "")
    status = str(definition.get("metric_definition_status") or definition.get("status") or "")
    formula = definition.get("metric_formula") or definition.get("formula")
    source = definition.get("metric_definition_source") or definition.get("sources") or []
    if (status.startswith("resolved") or source or formula) and label and decision_metric:
        source_text = f"，证据：{_format_list(source, 3)}" if source else ""
        formula_text = f"，公式：{formula}" if formula else ""
        return f"主目标指标口径：`{label}` 已由 Agent1 根据源码解析为 `{decision_metric}`{formula_text}{source_text}。"
    if status == "unresolved" and label:
        return f"主目标指标口径：`{label}` 未在源码中解析到可靠定义，本轮不能猜测该指标等价于哪个内部指标。"
    return ""


def _display_primary_label(label: Any, context: dict[str, Any]) -> str:
    text = str(label)
    definition = _metric_definition(context)
    decision_metric = str(definition.get("decision_metric") or "")
    status = str(definition.get("metric_definition_status") or definition.get("status") or "")
    if (status.startswith("resolved") or definition.get("metric_definition_source") or definition.get("sources")) and decision_metric and text.lower() != decision_metric.lower():
        return f"{text}（按 Agent1 源码解析映射到 {decision_metric}）"
    if status == "unresolved":
        return f"{text}（指标口径未解析）"
    return text


def _metric_definition(context: dict[str, Any]) -> dict[str, Any]:
    plan = context.get("experiment_plan") if isinstance(context.get("experiment_plan"), dict) else {}
    for value in (
        context.get("metric_definition"),
        plan.get("evaluation_metric"),
        plan.get("metric_definition"),
    ):
        if isinstance(value, dict) and value:
            return value
    return {}


def _format_list(values: Any, limit: int = 5) -> str:
    if not values:
        return "[]"
    if not isinstance(values, list):
        return str(values)
    shown = values[:limit]
    suffix = "" if len(values) <= limit else f" ... 共 {len(values)} 项"
    return f"{shown}{suffix}"


def _scan_samples(scan: dict[str, Any]) -> str:
    samples: list[str] = []
    for key in ("code_files", "config_files", "log_files", "data_files"):
        samples.extend(scan.get(key, [])[:3])
    return _format_list(samples, 12)


def _translate_scene(scene: str) -> str:
    parts = [SCENE_LABELS.get(part, part) for part in scene.split(";") if part]
    return " / ".join(parts) if parts else scene


def _format_changes(changes: list[dict[str, Any]]) -> str:
    if not changes:
        return "[]"
    formatted = []
    for change in changes:
        formatted.append(
            {
                "action": change.get("action"),
                "feature_name": change.get("feature_name"),
                "cli_args": change.get("cli_args", []),
            }
        )
    return str(formatted)


def _execution_method_text(plan: dict[str, Any], run_status: dict[str, Any]) -> str:
    if run_status.get("train_returncode") == "agent2_code_generation_failed" or run_status.get("agent2_code_generation_success") is False:
        return "Agent2 复制原实验训练入口及 Python 依赖到 trial/code 后尝试生成代码；代码生成未通过编译或特征应用审计，已跳过训练，原实验源码不被覆盖。"
    if run_status.get("feature_application_success") is False:
        return "Agent2 复制原实验训练入口及 Python 依赖到 trial/code 后执行特征应用审计；审计未通过，已跳过训练，原实验源码不被覆盖。"
    if run_status.get("generated_train_path") or plan.get("generated_train_path"):
        return "Agent2 复制原实验训练入口及 Python 依赖到 trial/code，只在 trial 副本中应用本轮特征代码；原实验源码不被覆盖。"
    return "Agent2 在受控实验目录中应用特征策略并执行训练/评测；若接入真实业务实验，应只在 trial 目录生成训练副本，不覆盖原实验源码。"


def _feature_application_audit_text(context: dict[str, Any]) -> str:
    run_status = context.get("run_status") or {}
    audit = context.get("feature_application_audit") or {}
    audit_path = run_status.get("feature_application_audit_path") or audit.get("audit_path")
    if run_status.get("train_returncode") == "agent2_code_generation_failed" or run_status.get("agent2_code_generation_success") is False:
        record = run_status.get("agent2_code_modification_path") or "code/agent2_code_modification.yaml"
        return f"未通过，Agent2 代码生成失败，未产生通过编译和特征应用审计的 trial 代码；修改记录：{record}。"
    if run_status.get("feature_application_success") is False or audit.get("success") is False:
        failed = [
            str(item.get("feature_name"))
            for item in audit.get("features", [])
            if isinstance(item, dict) and item.get("applied") is False
        ]
        suffix = f"，未落地特征：{failed}" if failed else ""
        path_text = f"，审计文件：{audit_path}" if audit_path else ""
        return f"未通过，Agent2 未找到本轮特征被真实训练代码消费的证据{suffix}{path_text}。"
    if run_status.get("feature_application_success") is True or audit.get("success") is True:
        path_text = f"，审计文件：{audit_path}" if audit_path else ""
        return f"通过，已发现本轮特征存在可执行代码证据{path_text}。"
    return "未记录；旧产物可能没有执行 Agent2 特征应用审计。"


def _agent2_modified_files_text(context: dict[str, Any]) -> str:
    audit = context.get("feature_application_audit") or {}
    run_status = context.get("run_status") or {}
    files = audit.get("modified_files") or run_status.get("agent2_modified_files") or []
    if not files:
        return "未记录"
    return "、".join(f"trial 版 {Path(str(path)).name}" for path in files)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Template report helper for tests and validation only.")
    parser.add_argument("--context", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--suggestions-output")
    parser.add_argument(
        "--allow-template-helper",
        action="store_true",
        help="Write the deterministic helper report for tests; runtime should not pass this flag.",
    )
    args = parser.parse_args()

    context = json.loads(Path(args.context).read_text(encoding="utf-8"))
    if not args.allow_template_helper:
        raise RuntimeError(
            "forecast_report.md must be generated by Agent1 LLM; deterministic write_report.py is disabled for runtime."
        )
    write_template_report(context, args.report, args.suggestions_output)
    print(args.report)
    if args.suggestions_output:
        print(args.suggestions_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
