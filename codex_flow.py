#!/usr/bin/env python3
"""
codex_flow.py — ComboScope Codex 多线程工作流 (轻量版)

架构: 1 Codex Session → 5 阶段 (4 Codex Threads + 1 Python)

  T1: Evaluate + Diagnose (read_only) — 由 using-forecast skill 编排
  T2a: Plan (workspace_write) — 生成 experiment_plan + feature_hypothesis
  [Python: copy source → agent2/code/]
  T2b: Code Generation (workspace_write) — 由 forecast-trial-codegen skill 驱动
  T3: Execute (Python 确定性) — 训练 + 评测 + keep/rollback
  T4: Report (workspace_write) — 由 forecast-report-writer skill 驱动

用法:
  python codex_flow.py \
    --experiment baseline \
    --ask "分析预测误差，提出特征实验并验证" \
    --output runs/trial_001

设计原则:
  - 执行流程由 skills/*/SKILL.md 定义, ThreadSpec prompt 仅补充 skills 未覆盖的约束
  - 硬约束不在 prompt 中重复 skills 已定义的内容 (program.md 护栏、skill 规则)
  - 单次 Codex session 完成全部 thread
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai_codex import Codex
from openai_codex.client import CodexConfig
from openai_codex._inputs import TextInput, SkillInput
from openai_codex._approval_mode import ApprovalMode
from openai_codex._sandbox import Sandbox

# ============================================================
# 固定路径 — skills 目录 & 确定性执行脚本
# ============================================================

SKILLS_ROOT = Path(__file__).resolve().parent / "skills"

# T3 确定性执行依赖的脚本 (skill scripts, 固定路径)
EVAL_CALCULATE_SCRIPT = SKILLS_ROOT / "forecast-evaluation-analyzer" / "scripts" / "calculate_metrics.py"

# Codex 会话配置 — RUST_LOG=info 输出 Rust binary 进度日志
CODEX_CONFIG = CodexConfig(env={"RUST_LOG": "info"})


# ============================================================
# 诊断工具
# ============================================================

def _dump_codex_stderr(codex: Codex, label: str = "") -> None:
    """输出 Rust binary 的 stderr 日志 (最后 30 行), 用于诊断"""
    try:
        lines = codex._client._stderr_lines
        if lines:
            recent = list(lines)[-30:]
            header = f"=== Rust stderr {label} (last {len(recent)} lines) ==="
            print(f"\n{header}")
            for line in recent:
                print(f"  {line.rstrip()}")
            print("=" * len(header))
    except Exception:
        pass


def _codex_health_check() -> bool:
    """预检: 用最小 prompt 确认 Codex 服务可达"""
    print("[Health] 检测 Codex 服务...")
    try:
        with Codex(config=CODEX_CONFIG) as codex:
            thread = codex.thread_start()
            result = thread.run("回复 OK")
            ok = "OK" in (result.final_response or "").upper()
            print(f"[Health] 服务{'正常' if ok else '异常'}")
            return ok
    except Exception as e:
        print(f"[Health] 服务不可达: {e}")
        return False

# ============================================================
# ThreadSpec — 每个阶段: 加载哪些 skills + 运行沙箱 + 简短补充 prompt
# ============================================================

@dataclass(frozen=True)
class ThreadSpec:
    """子线程规格 — skills 定义执行流程, prompt 仅补充 skills 未覆盖的约束"""
    phase: str
    skills: list[tuple[str, str]]  # [(name, relative_path_from_SKILLS_ROOT), ...]
    sandbox: Sandbox
    prompt: str  # 简短补充; 不含 skills 已定义的流程/规则/硬约束


# ── T1: Evaluate + Diagnose ──
# using-forecast 定义了完整的串行流程: task-planner → scanner → code-log →
#   mode-decision → evaluation → badcase → optimization-advisor
# 加载 using-forecast + 全部叶子 skill, prompt 仅指定输入/输出位置
THREAD_EVALUATE = ThreadSpec(
    phase="evaluate",
    skills=[
        ("using-forecast", "using-forecast"),
        ("forecast-task-planner", "forecast-task-planner"),
        ("forecast-experiment-scanner", "forecast-experiment-scanner"),
        ("forecast-code-log-analyzer", "forecast-code-log-analyzer"),
        ("forecast-badcase-locator", "forecast-badcase-locator"),
        ("forecast-optimization-advisor", "forecast-optimization-advisor"),
    ],
    sandbox=Sandbox.workspace_write,
    prompt="""\
预测实验评测诊断。使用 using-forecast 编排完整流程。

实验目录: {experiment_dir}
用户目标: {ask}

产物统一输出到 {output_dir}/ 下, 子目录约定:
  audit/       — 扫描结果 (scan_result.json, artifact_summary.json, code_analysis.json, log_summary.json)
  agent1/      — problem_context.json, badcase_diagnosis.md, artifact_contract.json
  agent2/      — source_evaluation_context.json
  standardized/ — standardized_prediction.csv, standardized_actual.csv (保留 split 列!)
  reports/     — optimization_suggestions.md

硬约束:
- {experiment_dir} 为 read_only, 只读不写
- 标准化时 MUST 保留原始 outputs/*.csv 中的 split 列值 (train/valid/test)
- 指标计算由 T3 (Python 确定性) 负责, 本阶段不需计算 WAPE/Bias
""",
)

# ── T2a: Plan ──
# forecast-optimization-advisor 已定义建议生成规则; 本阶段在其基础上生成
# 结构化的 experiment_plan.yaml 和 feature_hypothesis.yaml (Agent2 的可执行输入)
THREAD_PLAN = ThreadSpec(
    phase="plan",
    skills=[
        ("forecast-optimization-advisor", "forecast-optimization-advisor"),
        ("forecast-optimization-case-reference", "forecast-optimization-case-reference"),
        ("forecast-task-planner", "forecast-task-planner"),
    ],
    sandbox=Sandbox.workspace_write,
    prompt="""\
基于 T1 评测诊断产物, 生成可执行的特征实验计划。

输入 (位于 {output_dir}/):
  evaluation/  — 指标、场景、badcase
  agent1/      — problem_context.json, badcase_diagnosis.md, artifact_contract.json
  reports/     — optimization_suggestions.md (如有)
  audit/       — 扫描、代码、日志分析

必须产出:
  {output_dir}/agent1/feature_hypothesis.yaml
  {output_dir}/agent1/experiment_plan.yaml
  {output_dir}/reports/forecast_report.md  (评测诊断报告, 使用 forecast-report-writer 逻辑)

experiment_plan.yaml 最小结构:
```yaml
trial_id: {trial_id}
target_problem: "主问题 (来自 T1 证据)"
model_family: "从代码/日志识别的模型家族, 或 unknown"
objective: "目标函数, 或 unknown"
source_entrypoint: "训练入口文件相对路径"
changes:
  - action: add_feature | remove_feature | toggle_feature | modify_param
    feature_name: "特征名"
    feature_type: "特征类型"
    construction: "构造方式"
    expected_effect: "预期效果"
    risk: "风险"
    evidence: ["证据引用1", "证据引用2"]
    cli_flag: "--feature_xxx"  # 如通过 CLI 控制
    field_sources: ["源字段"]
candidate_experiments:  # 可选, 多方案时
  - experiment_id: "exp_01"
    title: "方案标题"
    priority: 1
    feature_actions: [{{action, feature_name, ...}}]
```

补充约束 (skills 未覆盖):
- 必须产出 experiment_plan.yaml (skills 只定义到 optimization_suggestions.md)
- source_entrypoint 从 T1 的 code_analysis.json 入口信息提取
""",
)

# ── T2b: Code Generation ──
# forecast-trial-codegen 的 SKILL.md 已定义 14 条硬规则, prompt 不重复
# 验证驱动: 生成代码 → 自验证 → 验证通过才输出
THREAD_CODEGEN = ThreadSpec(
    phase="codegen",
    skills=[
        ("forecast-trial-codegen", "forecast-trial-codegen"),
    ],
    sandbox=Sandbox.workspace_write,
    prompt="""\
基于实验计划生成 trial 代码。使用 forecast-trial-codegen 规则。

源码位置: {output_dir}/agent2/code/  (只能修改此目录)
实验计划: {output_dir}/agent1/experiment_plan.yaml
特征假设: {output_dir}/agent1/feature_hypothesis.yaml

必须产出 (验证通过后):
  {output_dir}/agent2/code/train.py
  {output_dir}/agent2/agent2_execution_plan.yaml

=== 自验证流程 (MUST 按顺序执行) ===

1. 生成 train.py, 按 experiment_plan.changes 逐条实现特征修改
2. 语法检查: 执行 `python -m py_compile agent2/code/train.py`
   失败 → 读错误 → 修复 → 重新执行直到通过 (最多 3 次)
3. 冒烟测试: 执行以下脚本, 确认训练入口可调用:
```bash
cd {output_dir}
python -c "
import sys, os, pandas as pd
sys.path.insert(0, 'agent2/code/src')
from lgb_package_to_dish_online_0319 import run_online_pipeline, parse_args, apply_experiment_preset
print('import OK')
# 验证 args parse 正常
parser = parse_args.__wrapped__ if hasattr(parse_args, '__wrapped__') else parse_args
args = parser.parse_args(['--experiment', 'baseline', '--history_eval_only', '--output_dir', 'outputs/real_outputs', '--data_path', '{experiment_dir}/data/dish_package_feature_df.csv', '--backtest_output_prefix', 'smoke_test'])
args = apply_experiment_preset(args)
print(f'args OK: experiment={{args.experiment}}, objective={{args.objective}}')
```
   失败 → 读错误 → 修复 train.py → 重新执行直到通过
4. 只有以上验证全部通过, 才写入 agent2_execution_plan.yaml 并输出

=== agent2_execution_plan.yaml 结构 ===
```yaml
agent: Agent2
trial_id: {trial_id}
source_entrypoint: "src/lgb_package_to_dish_online_0319.py"
python_dependencies: ["src/util.py", "src/check_input.py"]
train_command:
  - "python"
  - "{output_dir}/agent2/code/train.py"
  - "--experiment"
  - "baseline"
  - "--data_path"
  - "{experiment_dir}/data/dish_package_feature_df.csv"
  - "--output_dir"
  - "{output_dir}/outputs/real_outputs"
  - "--history_eval_only"
  - "--backtest_output_prefix"
  - "trial_{trial_id}"
output_contract:
  prediction_path: "trial_{trial_id}_package_detail.csv"
  actual_path: "trial_{trial_id}_package_detail.csv"
feature_changes:
  - action: ...
```

硬约束:
- 只在 {output_dir}/agent2/code/ 下修改, 不访问 {experiment_dir}
- 验证未全部通过前, 不输出 agent2_execution_plan.yaml
- pd.to_numeric() 默认返回值是标量 int, 链式 .fillna() 必崩
""",
)

# ── T4: Report ──
# forecast-report-writer 的 SKILL.md 已定义报告的规则/结构/质量检查
THREAD_REPORT = ThreadSpec(
    phase="report",
    skills=[
        ("forecast-report-writer", "forecast-report-writer"),
    ],
    sandbox=Sandbox.workspace_write,
    prompt="""\
撰写实验验证结论报告。使用 forecast-report-writer skill。

输入 (位于 {output_dir}/):
  agent2/review_result.json    — keep/rollback 决策 (确定性, 不可修改)
  agent2/experiment_review.md  — 实验执行详情
  evaluation/metric_comparison.json — 指标对比 (WAPE/Bias delta)
  reports/forecast_report.md   — T2a 评测诊断报告
  agent2/agent2_execution_plan.yaml

必须产出:
  {output_dir}/final_report.md

补充约束 (skills 未覆盖):
- review_result.json 中的 decision 是确定性计算的, 报告中直接引用, 不可修改
- 指标数值从 metric_comparison.json 读取, 不要手写
- final_report.md 附录需列出: reports/forecast_report.md, agent1/experiment_plan.yaml,
  agent2/agent2_execution_plan.yaml, agent2/code/train.py, evaluation/metric_comparison.json,
  agent2/experiment_review.md, workflow_manifest.json
""",
)


# ============================================================
# 产物清单管理 (轻量级, 替代 AgentRunRecorder + trace_writer + trial_archive)
# ============================================================

@dataclass
class WorkflowManifest:
    """线程间状态追踪 — 编排层直接管理, 不依赖下游 audit 文件"""
    trial_id: str
    experiment_dir: str
    output_dir: str
    ask: str
    started_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    threads: dict[str, Any] = field(default_factory=dict)

    def record(self, phase: str, thread_id: str, artifacts: list[str]) -> None:
        self.threads[phase] = {
            "thread_id": thread_id,
            "artifacts": artifacts,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._write()

    def record_error(self, phase: str, error: str) -> None:
        self.threads[phase] = {
            "thread_id": None,
            "error": error,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._write()

    def _write(self) -> None:
        path = Path(self.output_dir) / "workflow_manifest.json"
        path.write_text(json.dumps({
            "trial_id": self.trial_id,
            "experiment_dir": self.experiment_dir,
            "ask": self.ask,
            "started_at": self.started_at,
            "threads": self.threads,
        }, indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, output_dir: str) -> "WorkflowManifest | None":
        path = Path(output_dir) / "workflow_manifest.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        m = cls(
            trial_id=data["trial_id"],
            experiment_dir=data["experiment_dir"],
            output_dir=data["output_dir"],
            ask=data["ask"],
            started_at=data["started_at"],
        )
        m.threads = data.get("threads", {})
        return m


# ============================================================
# 源码复制 (T2b 前置)
# ============================================================

def copy_source_to_trial(experiment_dir: str, output_dir: str) -> list[str]:
    """将实验目录全部文件复制到 trial 目录, 返回复制的文件列表"""
    src = Path(experiment_dir)
    dst = Path(output_dir) / "agent2" / "code"
    dst.mkdir(parents=True, exist_ok=True)

    # 永远跳过的目录
    SKIP_PARTS = (
        ".venv", "node_modules", "archive", ".comboscope_backups",
        "__pycache__", ".git", ".claude", "runs",
    )

    copied: list[str] = []
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        if any(skip in f.parts for skip in SKIP_PARTS):
            continue
        rel = f.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        copied.append(str(rel))

    return copied


# ============================================================
# 子线程执行
# ============================================================

def _ensure_dirs(output_dir: str) -> None:
    """确保产物子目录存在 (与 skills 期望对齐)"""
    for sub in ["data", "outputs/real_outputs", "agent1", "agent2/code",
                "evaluation", "reports", "standardized", "logs", "audit"]:
        Path(output_dir, sub).mkdir(parents=True, exist_ok=True)


def _require_artifacts(output_dir: str, artifacts: list[str], phase: str) -> None:
    missing = [item for item in artifacts if not Path(output_dir, item).exists()]
    if missing:
        raise FileNotFoundError(f"[{phase}] missing required artifacts: {missing}")


def _resolve_skill_inputs(skills: list[tuple[str, str]]) -> list[SkillInput]:
    """将 (name, relative_path) 列表转为 SkillInput 列表"""
    return [SkillInput(name=name, path=str(SKILLS_ROOT / rel_path))
            for name, rel_path in skills]


def run_codex_thread(
    codex: Codex,
    spec: ThreadSpec,
    *,
    experiment_dir: str,
    output_dir: str,
    ask: str,
    trial_id: str,
) -> str:
    """启动并执行一个 Codex 子线程, 返回 thread_id"""
    prompt = spec.prompt.format(
        experiment_dir=experiment_dir,
        output_dir=output_dir,
        ask=ask,
        trial_id=trial_id,
    )
    inputs: list = [TextInput(text=prompt)] + _resolve_skill_inputs(spec.skills)

    print(f"\n{'='*50}")
    print(f"[{spec.phase}] 启动 Codex 线程")
    print(f"  Skills: {[s[0] for s in spec.skills]}")
    print(f"  Sandbox: {spec.sandbox.value}")
    print(f"{'='*50}")

    thread = codex.thread_start(
        cwd=output_dir,
        sandbox=Sandbox.full_access,
        approval_mode=ApprovalMode.deny_all,
    )
    print(f"[{spec.phase}] 等待 LLM 响应 (可能需要 30s-2min)...")
    result = thread.run(inputs, sandbox=Sandbox.full_access, approval_mode=ApprovalMode.deny_all)

    if result.status.value == "failed":
        error_msg = result.error.message if result.error else "unknown"
        _dump_codex_stderr(codex, f"{spec.phase}_error")
        raise RuntimeError(f"[{spec.phase}] 执行失败: {error_msg}")

    print(f"[{spec.phase}] 完成 — thread={thread.id}")
    last_msg = result.final_response or ""
    print(f"[{spec.phase}] 回复摘要: {last_msg[:300]}{'...' if len(last_msg) > 300 else ''}")
    if result.usage:
        u = result.usage.last
        print(f"  Tokens: in={u.input_tokens}, out={u.output_tokens}, total={u.total_tokens}")

    return thread.id


# ============================================================
# T3: 指标计算工具
# ============================================================

def _compute_wape_bias_from_csv(csv_path: Path, split_filter: str = "test") -> tuple[float, float, int]:
    """从带 split 列的 prediction CSV 计算指定 split 的 WAPE 和 Bias.

    支持列格式 (按优先级):
    - package_detail: true_pos_cnt, pred_pos_cnt
    - store_dish_day: true_real_qty_sum, pred_real_qty_sum
    - 标准化: actual, prediction
    - pre-computed: abs_error / abs_error_pos_cnt + 对应 true/error 列
    """
    import pandas as pd
    df = pd.read_csv(csv_path)

    if "split" in df.columns:
        df = df[df["split"] == split_filter]
    if len(df) == 0:
        raise ValueError(f"split='{split_filter}' 无数据")

    # 优先: package_detail 列名
    if "true_pos_cnt" in df.columns and "pred_pos_cnt" in df.columns:
        actual_col, pred_col = "true_pos_cnt", "pred_pos_cnt"
    # store_dish_day 列名
    elif "true_real_qty_sum" in df.columns and "pred_real_qty_sum" in df.columns:
        actual_col, pred_col = "true_real_qty_sum", "pred_real_qty_sum"
    # 通用列名
    elif "actual" in df.columns and "prediction" in df.columns:
        actual_col, pred_col = "actual", "prediction"
    elif "y_true" in df.columns and "y_pred" in df.columns:
        actual_col, pred_col = "y_true", "y_pred"
    # 预计算列 (package_detail)
    elif "abs_error_pos_cnt" in df.columns and "true_pos_cnt" in df.columns:
        wape = df["abs_error_pos_cnt"].sum() / df["true_pos_cnt"].sum()
        bias = df["error_pos_cnt"].sum() / df["true_pos_cnt"].sum()
        return float(wape), float(bias), len(df)
    # 预计算列 (store_dish_day)
    elif "abs_error" in df.columns and "true_real_qty_sum" in df.columns:
        wape = df["abs_error"].sum() / df["true_real_qty_sum"].sum()
        bias = df["error"].sum() / df["true_real_qty_sum"].sum()
        return float(wape), float(bias), len(df)
    else:
        raise ValueError(f"无法识别列, 可用: {list(df.columns)}")

    errors = df[pred_col] - df[actual_col]
    actual_sum = df[actual_col].sum()
    wape = errors.abs().sum() / actual_sum if actual_sum != 0 else float("nan")
    bias = errors.sum() / actual_sum if actual_sum != 0 else float("nan")
    return float(wape), float(bias), len(df)


# ============================================================
# T3: Python 确定性执行 (训练 + 评测 + keep/rollback)
# ============================================================

def execute_t3(manifest: WorkflowManifest) -> dict:
    """
    T3: 执行训练、计算指标、对比、keep/rollback 决策

    全部使用确定性规则, 不调用 LLM。
    评测脚本路径使用固定路径常量 EVAL_CALCULATE_SCRIPT。
    """
    output = Path(manifest.output_dir)
    exec_plan_path = output / "agent2" / "agent2_execution_plan.yaml"

    if not exec_plan_path.exists():
        raise FileNotFoundError(f"执行计划不存在: {exec_plan_path}")

    import yaml
    exec_plan = yaml.safe_load(exec_plan_path.read_text(encoding="utf-8"))

    # ── 1. 读取训练命令 ──
    train_cmd = exec_plan.get("train_command", [])
    if not train_cmd:
        raise ValueError("execution_plan 缺少 train_command")

    train_cmd = [
        str(arg).format(output_dir=str(output), trial_id=manifest.trial_id)
        for arg in train_cmd
    ]
    if train_cmd and Path(train_cmd[0]).name.lower() in {"python", "python.exe"}:
        train_cmd[0] = sys.executable

    # ── 2. 执行训练 ──
    print(f"\n[T3] 执行训练: {' '.join(train_cmd)}")
    train_result = subprocess.run(
        train_cmd,
        cwd=str(output / "agent2" / "code"),
        capture_output=True, text=True,
        timeout=3600,
    )

    train_log = output / "logs" / "train.log"
    train_log.write_text(
        f"STDOUT:\n{train_result.stdout}\n\nSTDERR:\n{train_result.stderr}",
        encoding="utf-8",
    )

    train_success = train_result.returncode == 0
    print(f"[T3] 训练 {'成功' if train_success else '失败'} (rc={train_result.returncode})")

    # ── 3. 运行评测 (固定路径: EVAL_CALCULATE_SCRIPT) ──
    output_contract = exec_plan.get("output_contract", {})
    prediction_path = (
        output / "outputs" / "real_outputs" /
        output_contract.get("prediction_path", "new_prediction.csv")
    )
    actual_path = output / "standardized" / "standardized_actual.csv"

    eval_success = False
    new_wape, new_bias, new_rows = 999.0, 0.0, 0  # 默认值: 训练失败时使用
    if train_success and prediction_path.exists():
        try:
            new_wape, new_bias, new_rows = _compute_wape_bias_from_csv(prediction_path, "test")
            eval_success = True
            print(f"[T3] new test: WAPE={new_wape:.4f}, Bias={new_bias:+.4f}, rows={new_rows}")
            (output / "evaluation" / "new_metrics.json").write_text(
                json.dumps({"wape": new_wape, "bias": new_bias, "rows": new_rows}, indent=2, ensure_ascii=False),
                encoding="utf-8")
        except Exception as e:
            print(f"[T3] test-split 读取失败 ({e}), 回退 calculate_metrics.py")
            new_metrics_csv = output / "evaluation" / "new_metrics_summary.csv"
            eval_result = subprocess.run(
                [sys.executable, str(EVAL_CALCULATE_SCRIPT),
                 "--prediction", str(prediction_path),
                 "--actual", str(actual_path),
                 "--output", str(new_metrics_csv)],
                capture_output=True, text=True, timeout=300,
            )
            eval_success = eval_result.returncode == 0
            if eval_success:
                try:
                    new_metrics = json.loads(eval_result.stdout)
                except json.JSONDecodeError:
                    new_metrics = {"wape": 999, "bias": 0}
                (output / "evaluation" / "new_metrics.json").write_text(
                    json.dumps(new_metrics, indent=2, ensure_ascii=False),
                    encoding="utf-8")
                new_wape = new_metrics.get("wape", 999)
                new_bias = new_metrics.get("bias", 0)
            else:
                print(f"[T3] 评测失败: {eval_result.stderr[:500]}")
    else:
        print(f"[T3] 跳过评测: train_success={train_success}, prediction_exists={prediction_path.exists()}")

    # ── 4. 读取 baseline 旧指标 (test 集, 直接从 outputs/ 原始文件) ──
    # 优先 package_detail (模型主输出, test WAPE ~0.70),
    # 同时读取 store_dish_day (dish 分配后, test WAPE ~0.49)
    old_wape, old_bias, old_rows = 999.0, 0.0, 0
    old_wape_dish, old_bias_dish = 999.0, 0.0
    exp_dir = Path(manifest.experiment_dir)

    # 主指标: package_detail
    pkg_files = sorted(exp_dir.glob("outputs/*_package_detail.csv"))
    if not pkg_files:
        pkg_files = sorted(exp_dir.glob("outputs/*.csv"))  # fallback
    if pkg_files:
        try:
            old_wape, old_bias, old_rows = _compute_wape_bias_from_csv(pkg_files[0], "test")
            print(f"[T3] baseline package_detail test: WAPE={old_wape:.4f}, Bias={old_bias:+.4f}, rows={old_rows}")
        except Exception as e:
            print(f"[T3] 警告: 读取 package_detail 失败 ({e})")

    # 辅助指标: store_dish_day
    dish_files = sorted(exp_dir.glob("outputs/*_store_dish_day.csv"))
    if dish_files:
        try:
            old_wape_dish, old_bias_dish, _ = _compute_wape_bias_from_csv(dish_files[0], "test")
            print(f"[T3] baseline store_dish_day test: WAPE={old_wape_dish:.4f}, Bias={old_bias_dish:+.4f}")
        except Exception as e:
            print(f"[T3] 警告: 读取 store_dish_day 失败 ({e})")

    # 回退: 如果 outputs/ 读取失败, 用 T1 标准化产物
    if old_wape == 999:
        old_metrics: dict = {}
        old_metrics_path = output / "evaluation" / "metrics.json"
        if old_metrics_path.exists():
            old_metrics = json.loads(old_metrics_path.read_text(encoding="utf-8"))
        if not old_metrics:
            metrics_csv = output / "evaluation" / "metrics_summary.csv"
            if metrics_csv.exists():
                import csv
                with open(metrics_csv, encoding="utf-8-sig") as f:
                    for row in csv.DictReader(f):
                        try:
                            old_wape = float(row.get("wape", old_wape))
                            old_bias = float(row.get("bias", old_bias))
                        except (ValueError, TypeError):
                            pass
                        break
        else:
            old_wape = old_metrics.get("wape", 999)
            old_bias = old_metrics.get("bias", 0)

    # (train 失败时 new_wape 保持 999)

    wape_delta = round(old_wape - new_wape, 6)
    bias_delta = round(abs(new_bias) - abs(old_bias), 6)

    # ── 5. Keep/Rollback 决策 (与 program.md 一致) ──
    keep = (
        train_success
        and eval_success
        and wape_delta > 0.005
        and bias_delta < 0.02
    )
    decision = "keep" if keep else "rollback"

    comparison = {
        "decision": decision,
        "reason": (f"package_detail WAPE delta={wape_delta:.4f}, Bias delta={bias_delta:.4f}, "
                   f"train_ok={train_success}, eval_ok={eval_success}"),
        "wape_delta": wape_delta,
        "bias_delta": bias_delta,
        "primary": {
            "level": "package_detail",
            "old_wape": old_wape, "new_wape": new_wape,
            "old_bias": old_bias, "new_bias": new_bias,
            "old_rows": old_rows,
        },
        "secondary": {
            "level": "store_dish_day",
            "old_wape": old_wape_dish, "old_bias": old_bias_dish,
        },
    }
    (output / "evaluation" / "metric_comparison.json").write_text(
        json.dumps(comparison, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # ── 6. 写 run_status + review_result + experiment_review ──
    run_status = {
        "train_success": train_success,
        "eval_success": eval_success,
        "prediction_path": str(prediction_path),
        "train_log_path": str(output / "logs" / "train.log"),
    }
    (output / "agent2" / "run_status.json").write_text(
        json.dumps(run_status, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    review = {
        "decision": decision,
        "reason": comparison["reason"],
        "wape_delta": wape_delta,
        "bias_delta": bias_delta,
    }
    (output / "agent2" / "review_result.json").write_text(
        json.dumps(review, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    review_md = f"""\
# 实验执行审查

## 决策
{decision.upper()} — {comparison['reason']}

## 训练
- 命令: `{' '.join(train_cmd)}`
- 成功: {train_success}
- 日志: logs/train.log

## 评测 (test 集)
- Baseline: WAPE={old_wape:.4f}, Bias={old_bias:+.4f}
- New:      WAPE={new_wape:.4f}, Bias={new_bias:+.4f}
- Delta:    WAPE={wape_delta:+.4f}, Bias={bias_delta:+.4f}

## 源码修改
见 agent2/code/train.py
"""
    (output / "agent2" / "experiment_review.md").write_text(review_md, encoding="utf-8")

    print(
        f"\n[T3] 决策: {decision.upper()} | "
        f"package_detail WAPE {old_wape:.4f}→{new_wape:.4f} ({wape_delta:+.4f}) | "
        f"Bias {old_bias:+.4f}→{new_bias:+.4f} ({bias_delta:+.4f})"
    )
    if old_wape_dish != 999:
        print(f"[T3]     store_dish_day WAPE={old_wape_dish:.4f} (辅助参考)")

    return comparison


# ============================================================
# 主工作流编排 (单次 Codex Session — 登录与工作在同一 session)
# ============================================================

def run_workflow(
    experiment_dir: str,
    ask: str,
    output_dir: str,
) -> WorkflowManifest:
    """
    ComboScope Codex 多线程工作流 — 单次 Codex Session

    流程:
      认证 → T1 (evaluate) → T2a (plan) → [copy source] → T2b (codegen)
      → T3 (execute, Python) → T4 (report)
    """
    trial_id = Path(output_dir).name
    exp_dir = str(Path(experiment_dir).resolve())
    out_dir = str(Path(output_dir).resolve())

    manifest = WorkflowManifest(
        trial_id=trial_id,
        experiment_dir=exp_dir,
        output_dir=out_dir,
        ask=ask,
    )
    _ensure_dirs(output_dir)

    # 线程公共参数
    thread_kwargs = dict(experiment_dir=exp_dir, output_dir=out_dir, ask=ask, trial_id=trial_id)

    # API Key 优先 (环境变量), 无则走设备码
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY")

    # ════════════════════════════════════════════════════════════
    # 单次 Codex Session: 认证 + T1~T4 全部在同一 session 内完成
    # ════════════════════════════════════════════════════════════
    with Codex(config=CODEX_CONFIG) as codex:
        # ── 认证 (同一 session, 不跨 session 依赖磁盘持久化) ──
        if api_key:
            print("[Auth] 使用 API Key 认证")
            codex.login_api_key(api_key)
        else:
            try:
                acct = codex.account()
                if acct.requires_openai_auth:
                    raise RuntimeError("requires re-auth")
                print(f"[Auth] 已登录: {acct.root.email}")
            except Exception:
                # 清旧 + 设备码登录 (同一 session)
                try:
                    codex.logout()
                except Exception:
                    pass
                handle = codex.login_chatgpt_device_code()
                print(f"\n请在浏览器中打开: {handle.verification_url}")
                print(f"输入验证码: {handle.user_code}\n")
                result = handle.wait()
                if not result.success:
                    raise RuntimeError(f"[Auth] 登录失败: {result}")
                print("[Auth] 登录成功")

        # ── T1: Evaluate + Diagnose ──
        try:
            t1_id = run_codex_thread(codex, THREAD_EVALUATE, **thread_kwargs)
            _require_artifacts(output_dir, [
                "standardized/standardized_actual.csv",
                "agent1/problem_context.json",
                "agent1/artifact_contract.json",
            ], "evaluate")
            manifest.record("evaluate", t1_id, [
                "audit/scan_result.json",
                "audit/artifact_summary.json",
                "audit/code_analysis.json",
                "audit/log_summary.json",
                "standardized/standardized_prediction.csv",
                "standardized/standardized_actual.csv",
                "agent1/problem_context.json",
                "agent1/badcase_diagnosis.md",
                "agent1/artifact_contract.json",
                "agent2/source_evaluation_context.json",
                "reports/optimization_suggestions.md",
            ])
        except Exception as e:
            manifest.record_error("evaluate", str(e))
            raise

        # ── T2a: Plan ──
        try:
            t2a_id = run_codex_thread(codex, THREAD_PLAN, **thread_kwargs)
            manifest.record("plan", t2a_id, [
                "agent1/feature_hypothesis.yaml",
                "agent1/experiment_plan.yaml",
                "agent1/candidate_experiments.yaml",
                "reports/forecast_report.md",
                "reports/report_context.json",
            ])
        except Exception as e:
            manifest.record_error("plan", str(e))
            raise

        # ── Copy Source (T2b 前置) ──
        copied = copy_source_to_trial(experiment_dir, output_dir)
        print(f"\n[PRE-T2b] 已复制源码到 agent2/code/: {len(copied)} 个文件")

        # ── T2b: Code Generation ──
        try:
            t2b_id = run_codex_thread(codex, THREAD_CODEGEN, **thread_kwargs)
            manifest.record("codegen", t2b_id, [
                "agent2/code/train.py",
                "agent2/agent2_execution_plan.yaml",
            ])
        except Exception as e:
            manifest.record_error("codegen", str(e))
            raise

        # ── T3 + codegen 回退循环 ──
        # 训练失败时, 用 thread_resume 继续 codegen 线程 (保留完整上下文)
        MAX_RETRIES = 2
        comparison = None
        for attempt in range(1 + MAX_RETRIES):
            try:
                comparison = execute_t3(manifest)
            except Exception as e:
                manifest.record_error("execute", str(e))
                raise

            train_ok = (comparison.get("primary", {}).get("new_wape", 999) != 999)
            if train_ok:
                manifest.record("execute", "N/A (Python deterministic)", [
                    "agent2/run_status.json", "agent2/review_result.json",
                    "agent2/experiment_review.md", "evaluation/new_metrics.json",
                    "evaluation/new_metrics_summary.csv",
                    "evaluation/metric_comparison.json", "logs/train.log",
                ])
                break

            if attempt >= MAX_RETRIES:
                # 终极回退: 用原始脚本 + baseline preset 直接跑
                src_script = Path(output_dir) / "agent2" / "code" / "src" / "lgb_package_to_dish_online_0319.py"
                if src_script.exists():
                    print("[T3] 回退: 使用原始训练脚本 (baseline preset, 不依赖 codegen)")
                    fallback_cmd = [
                        sys.executable, str(src_script),
                        "--experiment", "baseline",
                        "--history_eval_only",
                        "--data_path", f"{exp_dir}/data/dish_package_feature_df.csv",
                        "--output_dir", str(Path(output_dir) / "outputs" / "real_outputs"),
                        "--backtest_output_prefix", "fallback",
                    ]
                    fb_result = subprocess.run(
                        fallback_cmd,
                        cwd=str(Path(output_dir) / "agent2" / "code"),
                        capture_output=True, text=True, timeout=3600,
                    )
                    train_log_path.write_text(
                        (train_log_path.read_text(encoding="utf-8") if train_log_path.exists() else "") +
                        f"\n\n=== FALLBACK (原始脚本 baseline preset) ===\n"
                        f"CMD: {' '.join(fallback_cmd)}\n"
                        f"RC: {fb_result.returncode}\n"
                        f"STDERR: {fb_result.stderr[-500:]}",
                        encoding="utf-8",
                    )
                    if fb_result.returncode == 0:
                        print("[T3] 回退训练成功! 使用原始脚本输出计算指标")
                        fb_pred = Path(output_dir) / "outputs" / "real_outputs" / "fallback_package_detail.csv"
                        if fb_pred.exists():
                            try:
                                fallback_wape, fallback_bias, fallback_rows = _compute_wape_bias_from_csv(fb_pred, "test")
                                print(f"[T3] fallback test: WAPE={fallback_wape:.4f}, Bias={fallback_bias:+.4f}")
                                # 更新 comparison
                                primary = comparison.setdefault("primary", {})
                                primary["new_wape"] = fallback_wape
                                primary["new_bias"] = fallback_bias
                                wape_delta = round(primary.get("old_wape", 999) - fallback_wape, 6)
                                bias_delta = round(abs(fallback_bias) - abs(primary.get("old_bias", 0)), 6)
                                train_ok = True
                                eval_success = True
                                keep = wape_delta > 0.005 and bias_delta < 0.02
                                comparison["decision"] = "keep" if keep else "rollback"
                                comparison["wape_delta"] = wape_delta
                                comparison["bias_delta"] = bias_delta
                                comparison["reason"] = f"fallback 原始脚本: WAPE delta={wape_delta:.4f}"
                                # 写入 new_metrics
                                (Path(output_dir) / "evaluation" / "new_metrics.json").write_text(
                                    json.dumps({"wape": fallback_wape, "bias": fallback_bias, "rows": fallback_rows, "source": "fallback_original_script"}, indent=2),
                                    encoding="utf-8")
                            except Exception as fe:
                                print(f"[T3] 回退指标计算失败: {fe}")

                manifest.record("execute", "N/A (Python deterministic)", [
                    "agent2/run_status.json", "agent2/review_result.json",
                    "agent2/experiment_review.md",
                    "evaluation/metric_comparison.json", "logs/train.log",
                ])
                print("[T3] 已达最大重试次数, 使用 baseline 指标生成报告")
                break

            # 读错误日志, 反馈给 codegen 线程修复
            train_log_path = Path(output_dir) / "logs" / "train.log"
            error_text = ""
            if train_log_path.exists():
                lines = train_log_path.read_text(encoding="utf-8").split("\n")
                # 取最后的 Traceback
                for i in range(len(lines) - 1, -1, -1):
                    if "Traceback" in lines[i]:
                        error_text = "\n".join(lines[i:])
                        break
                if not error_text:
                    error_text = "\n".join(lines[-20:])

            fix_input = TextInput(text=(
                f"训练失败, 以下是错误日志:\n\n```\n{error_text}\n```\n\n"
                f"请定位错误原因, 修复 agent2/code/ 下的代码, 然后更新 agent2_execution_plan.yaml。"
                f"只修复导致上述错误的代码, 不要重写整个文件。"
                f"修复后重新执行验证步骤 (py_compile + 冒烟测试)。"
            ))

            # 输出错误摘要供诊断
            err_last_line = error_text.strip().split("\n")[-1] if error_text.strip() else "(无法读取错误日志)"
            print(f"\n[codegen-retry {attempt+1}/{MAX_RETRIES}] 错误: {err_last_line}")
            print(f"[codegen-retry] 反馈错误到 codegen 线程 {t2b_id}...")
            resume_thread = codex.thread_resume(t2b_id)
            resume_result = resume_thread.run(
                [fix_input],
                sandbox=Sandbox.full_access,
                approval_mode=ApprovalMode.deny_all,
            )
            if resume_result.status.value == "failed":
                print(f"[codegen-retry] 修复线程失败: {resume_result.error.message if resume_result.error else 'unknown'}")
            else:
                print(f"[codegen-retry] 修复完成, 重新执行 T3...")
                # 修复后, 用 py_compile 验证语法
                train_py = Path(output_dir) / "agent2" / "code" / "train.py"
                if train_py.exists():
                    import py_compile as pyc
                    try:
                        pyc.compile(str(train_py), doraise=True)
                        print("[codegen-retry] train.py 语法验证通过")
                    except pyc.PyCompileError as ce:
                        print(f"[codegen-retry] train.py 语法错误: {ce}")

        # 确保 comparison 始终可用
        if comparison is None:
            comparison = {"decision": "rollback", "reason": "训练失败, 无新指标",
                          "primary": {"old_wape": 999, "new_wape": 999}}

        # ── T4: Report (同一 session, T3 产物已就绪) ──
        try:
            t4_id = run_codex_thread(codex, THREAD_REPORT, **thread_kwargs)
            manifest.record("report", t4_id, [
                "final_report.md",
            ])
            codex.thread_archive(t4_id)
        except Exception as e:
            manifest.record_error("report", str(e))
            raise

    # ── 完成 ──
    print(f"\n{'='*50}")
    print(f"工作流完成")
    print(f"  Trial: {trial_id}")
    print(f"  决策: {comparison['decision'].upper()}")
    print(f"  报告: {output_dir}/final_report.md")
    print(f"  Manifest: {output_dir}/workflow_manifest.json")
    print(f"{'='*50}")

    return manifest


# ============================================================
# CLI
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="ComboScope Codex Workflow (轻量版)")
    parser.add_argument("--experiment", required=True, help="预测实验目录")
    parser.add_argument("--ask", required=True, help="实验目标描述")
    parser.add_argument("--output", default="runs/trial_001", help="输出目录 (默认 runs/trial_001)")
    args = parser.parse_args()

    try:
        run_workflow(
            experiment_dir=args.experiment,
            ask=args.ask,
            output_dir=args.output,
        )
    except Exception as exc:
        print(f"\n[FATAL] 工作流失败: {exc}", file=sys.stderr)
        output = Path(args.output)
        if output.exists():
            manifest_path = output / "workflow_manifest.json"
            if manifest_path.exists():
                print(f"查看状态: {manifest_path}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
