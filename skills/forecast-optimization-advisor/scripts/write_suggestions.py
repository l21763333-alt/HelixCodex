from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read_json(path: str | Path | None, default: Any) -> Any:
    if not path:
        return default
    target = Path(path)
    if not target.exists():
        return default
    return json.loads(target.read_text(encoding="utf-8"))


def _read_csv_records(path: str | Path | None, limit: int = 20) -> list[dict[str, Any]]:
    if not path or not Path(path).exists():
        return []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        import csv
        return list(csv.DictReader(handle))[:limit]


def _add(items: list[dict[str, str]], title: str, evidence: str, action: str, validation: str) -> None:
    items.append({"title": title, "evidence": evidence, "action": action, "validation": validation})


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_suggestions(context: dict[str, Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    artifact = context.get("artifact_summary", {}) or {}
    missing = artifact.get("missing_artifacts") or []
    ambiguous = artifact.get("ambiguous_artifacts") or {}
    if missing:
        _add(
            items,
            "补齐关键评测产物",
            f"artifact_summary.missing_artifacts={missing}",
            "补充导出缺失文件，或用 CLI 覆盖参数显式传入对应路径。",
            "重新运行 discover_artifacts，missing_artifacts 应减少；prediction/actual 齐全后再进入 Full 模式。",
        )
    if ambiguous:
        _add(
            items,
            "消歧候选产物",
            f"artifact_summary.ambiguous_artifacts={ambiguous}",
            "重命名产物或显式指定路径，避免多个 prediction/actual/log 候选被工具判定为同等可信。",
            "重新扫描后 ambiguous_artifacts 为空，confidence 不再是 ambiguous。",
        )

    log_summary = context.get("log_summary", {}) or {}
    if log_summary.get("errors"):
        _add(
            items,
            "优先排查日志错误",
            f"log_summary.errors={log_summary.get('errors', [])[:3]}",
            "定位 error/exception/nan 所在阶段，先确认训练或评测是否完整产出。",
            "修复后重新解析日志，errors 清空或能够解释为非阻断信息。",
        )
    if log_summary.get("warnings"):
        _add(
            items,
            "复核日志 warning",
            f"log_summary.warnings={log_summary.get('warnings', [])[:3]}",
            "检查 warning 是否对应缺失特征、缺失样本或依赖兼容问题；不要直接当成根因。",
            "补充证据后确认 warning 与指标或 badcase 是否有关联。",
        )

    code_analysis = context.get("code_analysis", {}) or {}
    if code_analysis.get("metric_functions"):
        _add(
            items,
            "确认指标口径",
            f"code_analysis.metric_functions={code_analysis.get('metric_functions')}",
            "核对代码中的指标定义与本次评测脚本的 WAPE/MAPE/Bias 口径是否一致。",
            "用同一份 prediction/actual 对比两边指标，差异需要能被口径解释。",
        )
    if code_analysis.get("output_patterns") and not artifact.get("prediction_path"):
        _add(
            items,
            "确认预测输出路径",
            f"code_analysis.output_patterns={code_analysis.get('output_patterns')}",
            "沿输出线索确认预测文件实际落盘位置，并纳入 scanner 规则或手动覆盖路径。",
            "artifact_summary.prediction_path 能稳定指向真实预测文件。",
        )

    metrics = context.get("metrics") or []
    metric_map = {str(row.get("metric")): row.get("value") for row in metrics if isinstance(row, dict)}
    wape = metric_map.get("wape")
    bias = metric_map.get("bias")
    if wape is not None:
        try:
            if float(wape) >= 0.3:
                _add(
                    items,
                    "拆解高误差来源",
                    f"metrics.wape={wape}",
                    "优先结合 scene_metrics 和 badcases 找出高误差集中在哪些业务对象、日期、分组维度或目标值层级。",
                    "下一次评测中高误差场景的 WAPE 应下降，且不是由样本量变化单独造成。",
                )
        except (TypeError, ValueError):
            pass
    if bias is not None:
        try:
            if abs(float(bias)) >= 0.1:
                direction = "高估" if float(bias) > 0 else "低估"
                _add(
                    items,
                    "复核系统性偏差",
                    f"metrics.bias={bias}，方向={direction}",
                    "检查目标定义、促销/节假日特征、库存缺货和异常目标值处理是否导致整体偏移。",
                    "分层 Bias 收敛，且主要业务分组不再持续同向偏移。",
                )
        except (TypeError, ValueError):
            pass

    scene_metrics = _priority_scenes(context.get("scene_metrics") or [])
    if scene_metrics:
        top_scene = scene_metrics[0]
        _add(
            items,
            "按整体误差贡献复核场景",
            f"scene_metrics sample={top_scene}",
            "优先按 abs_error_sum/整体误差贡献回看样本覆盖、特征完整性和业务分布；WAPE 高但贡献小的切片不应压过贡献更大的场景。",
            "对应场景的样本量、actual_sum、WAPE/Bias 都有可解释结论。",
        )
        _add_specific_feature_experiments(
            items,
            metrics=metric_map,
            scene_metrics=scene_metrics,
            badcases=context.get("badcases") or [],
            code_analysis=context.get("code_analysis") or {},
        )

    badcases = context.get("badcases") or []
    if badcases:
        _add(
            items,
            "抽样核查 badcase",
            f"badcases sample={badcases[:3]}",
            "分别抽查最大绝对误差、最大 APE、低估和高估样本，确认是否存在数据口径或异常业务事件。",
            "每类 top badcase 都能标注可解释原因或进入后续数据修复清单。",
        )
        if not scene_metrics:
            _add_specific_feature_experiments(
                items,
                metrics=metric_map,
                scene_metrics=[],
                badcases=badcases,
                code_analysis=context.get("code_analysis") or {},
            )

    anomalies = (context.get("anomaly_summary") or {}).get("anomalies") or []
    for anomaly in anomalies[:3]:
        _add(
            items,
            "处理异常摘要信号",
            f"anomaly_summary={anomaly}",
            "围绕该异常补充日志、样本和场景证据；只把它作为排查线索，不直接写成根因。",
            "复跑 detect_anomalies 后该异常消失，或报告中能说明其业务含义。",
        )

    if not items:
        _add(
            items,
            "补充可操作证据",
            "当前上下文没有足够的 missing、ambiguous、日志、指标或 badcase 证据。",
            "先补齐 artifact_summary、log_summary、metrics 或 badcases，再生成更具体建议。",
            "下一次建议输出能引用至少一个结构化证据字段。",
        )
    if any(_is_concrete_feature_item(item) for item in items):
        items = [item for item in items if not _is_generic_modeling_item(item)]
    return items


def _add_specific_feature_experiments(
    items: list[dict[str, str]],
    *,
    metrics: dict[str, Any],
    scene_metrics: list[dict[str, Any]],
    badcases: list[dict[str, Any]],
    code_analysis: dict[str, Any],
) -> None:
    primary_metric = _primary_metric_label(metrics)
    scene_text = " | ".join(str(row.get("scene", "")) for row in scene_metrics[:3])
    badcase_text = " | ".join(
        ";".join(
            str(row.get(key, ""))
            for key in ["package_dish_name", "package_age_days", "days_to_end", "actual", "prediction"]
            if row.get(key) not in (None, "")
        )
        for row in badcases[:3]
    )
    contribution = _scene_contribution_evidence(scene_metrics)
    evidence = f"metrics={metrics}; priority_scene_by_abs_error={contribution}; badcase_sample={badcase_text}"
    capabilities = _source_capabilities(code_analysis)
    has_high_under = "high_target" in scene_text and "underestimate" in scene_text
    if has_high_under and capabilities["has_rolling_path"]:
        _add(
            items,
            "验证 extend_rolling_windows_14_30",
            evidence,
            "沿源码已有 rolling_windows -> build_features -> generate_rolling_features 链路，把窗口从当前短窗扩展到 14/30；不要新增源码不认识的 enable 开关。",
            f"复跑后确认 rolling_14/rolling_30 特征进入 feature_cols，再检查 {primary_metric}、整体 WAPE、high_target;underestimate Bias 和 normal_target;overestimate Bias。",
        )
    elif has_high_under:
        _add(
            items,
            "先定位 rolling 特征接入链路",
            evidence,
            "先从源码确认是否存在 rolling window 参数、build_features 或特征列返回路径；没有消费链路时不要把新 feature flag 写入 plan。",
            "Agent2 审计应能看到参数被解析并在训练特征路径消费，或看到新增特征列进入 feature_cols。",
        )
    if any(str(row.get("package_age_days", "")) not in {"", "nan"} for row in badcases) and not capabilities["has_lifecycle_path"]:
        _add(
            items,
            "构建 package_lifecycle_bucket",
            evidence,
            "基于 package_age_days 或等价生命周期字段构建新品/成长期/稳定期分桶，并明确加入 feature_cols；如果源码已有生命周期特征，只验证新增交叉项。",
            "按生命周期分桶复算 WAPE/Bias，确认新品或短生命周期套餐是否从 top badcase 中减少。",
        )
    if any(str(row.get("days_to_end", "")) not in {"", "nan"} for row in badcases) and not capabilities["has_days_to_end_path"]:
        _add(
            items,
            "构建 days_to_end_bucket",
            evidence,
            "把 days_to_end 或等价在线可得字段分成 0-3、4-7、8-14、15+ 天窗口，并与周末/工作日交叉；如果源码已有连续 days_to_end，只验证分桶/交叉增益。",
            "验证 days_to_end 较小或中等窗口的 badcase 绝对误差是否下降。",
        )
    if metrics.get("bias") is not None and capabilities["has_calibrator"]:
        _add(
            items,
            "复核 source_aligned_group_calibration",
            evidence,
            "复用源码已有 group calibrator，只调整预测时可得的分组字段；不要使用 underestimate/overestimate 这类事后误差标签，避免泄漏。",
            f"对比校准前后 {primary_metric}、high_target Bias 和整体 WAPE，若主目标没有改善则回滚。",
        )


def _priority_scenes(scene_metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        scene_metrics,
        key=lambda row: (
            _to_float(row.get("abs_error_sum")) if _to_float(row.get("abs_error_sum")) is not None else -1.0,
            _to_float(row.get("wape")) if _to_float(row.get("wape")) is not None else -1.0,
        ),
        reverse=True,
    )


def _scene_contribution_evidence(scene_metrics: list[dict[str, Any]]) -> str:
    if not scene_metrics:
        return ""
    return " | ".join(
        f"{row.get('scene')} abs_error_sum={row.get('abs_error_sum')} wape={row.get('wape')} bias={row.get('bias')}"
        for row in scene_metrics[:3]
    )


def _source_capabilities(code_analysis: dict[str, Any]) -> dict[str, bool]:
    values: list[str] = []
    for key in ("function_defs", "code_identifiers", "feature_functions", "feature_modules", "model_modules", "entrypoints"):
        raw = code_analysis.get(key) or []
        if isinstance(raw, list):
            values.extend(str(item) for item in raw)
    joined = " ".join(values).lower()
    cli_args = {str(item) for item in code_analysis.get("argparse_args", []) or [] if str(item).startswith("--")}
    has_rolling_path = _supports_cli(cli_args, "--rolling_windows") or (
        "build_features" in joined and ("generate_rolling_features" in joined or "rolling_windows" in joined)
    )
    return {
        "has_rolling_path": has_rolling_path,
        "has_lifecycle_path": "add_package_lifecycle_features" in joined or "package_lifecycle_bucket" in joined,
        "has_days_to_end_path": "days_to_end" in joined,
        "has_calibrator": "fit_group_calibrator" in joined or "calibrator" in joined,
    }


def _supports_cli(cli_args: set[str], flag: str) -> bool:
    body = flag[2:] if flag.startswith("--") else flag
    variants = {"--" + body, "--" + body.replace("_", "-"), "--" + body.replace("-", "_")}
    return bool(cli_args.intersection(variants))


def _primary_metric_label(metrics: dict[str, Any]) -> str:
    for key in ("primary_metric", "objective_label", "decision_metric"):
        value = metrics.get(key)
        if value:
            return str(value)
    return "用户 ask 指定的主指标"


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


def _is_generic_modeling_item(item: dict[str, str]) -> bool:
    return item.get("title") in {
        "拆解高误差来源",
        "复核系统性偏差",
        "按整体误差贡献复核场景",
        "抽样核查 badcase",
        "处理异常摘要信号",
    }


def render_markdown(items: list[dict[str, str]]) -> str:
    lines = ["# 优化建议", ""]
    for index, item in enumerate(items, start=1):
        lines.extend(
            [
                f"## 建议 {index}：{item['title']}",
                "",
                f"- 证据：{item['evidence']}",
                f"- 动作：{item['action']}",
                f"- 验证：{item['validation']}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Write evidence-based forecast suggestions from structured outputs.")
    parser.add_argument("--context")
    parser.add_argument("--artifact-summary")
    parser.add_argument("--log-summary")
    parser.add_argument("--code-analysis")
    parser.add_argument("--metrics")
    parser.add_argument("--scene-metrics")
    parser.add_argument("--badcases")
    parser.add_argument("--anomaly-summary")
    parser.add_argument("--output")
    args = parser.parse_args()

    context = _read_json(args.context, {})
    if args.artifact_summary:
        context["artifact_summary"] = _read_json(args.artifact_summary, {})
    if args.log_summary:
        context["log_summary"] = _read_json(args.log_summary, {})
    if args.code_analysis:
        context["code_analysis"] = _read_json(args.code_analysis, {})
    if args.metrics:
        context["metrics"] = _read_csv_records(args.metrics)
    if args.scene_metrics:
        context["scene_metrics"] = _read_csv_records(args.scene_metrics)
    if args.badcases:
        context["badcases"] = _read_csv_records(args.badcases)
    if args.anomaly_summary:
        context["anomaly_summary"] = _read_json(args.anomaly_summary, {})

    markdown = render_markdown(build_suggestions(context))
    if args.output:
        Path(args.output).write_text(markdown, encoding="utf-8")
    print(markdown, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
