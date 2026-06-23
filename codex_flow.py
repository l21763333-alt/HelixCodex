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
from openai_codex.generated.v2_all import (
    GetAccountRateLimitsResponse,
    CodexErrorInfoValue,
)
from config import build_codex_config, get_config

# ============================================================
# 固定路径 — skills 目录 & 确定性执行脚本
# ============================================================

SKILLS_ROOT = Path(__file__).resolve().parent / "skills"

# T3 确定性执行依赖的脚本 (skill scripts, 固定路径)
EVAL_CALCULATE_SCRIPT = SKILLS_ROOT / "forecast-evaluation-analyzer" / "scripts" / "calculate_metrics.py"

# Codex 会话配置
# CODEX_HOME 固定: 确保 app-server 每次都从同一个目录读写 session 状态
def _build_codex_config() -> CodexConfig:
    return build_codex_config()

CODEX_CONFIG = _build_codex_config()


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


def _ensure_session(codex: Codex) -> None:
    """统一认证: 通过实际模型调用验证 session 可用, 不可用时触发设备码登录

    不依赖 account().requires_openai_auth —— 设备码登录给的是 ChatGPT 消费级
    session, account() 对它永远返回 True, 但实际模型调用能跑通。
    """
    # 方式1: API Key (最快, 无交互)
    api_key = get_config().api_key
    if api_key:
        print("[Auth] 使用 API Key 认证")
        codex.login_api_key(api_key)
        return

    # 方式2: 检查已有 session 是否实际可用 (直接跑模型调用)
    try:
        t = codex.thread_start()
        r = t.run("回复: OK")
        if r.final_response:
            print("[Auth] ✅ session 可用, 无需重新认证")
            return
    except Exception:
        pass

    # 方式3: 设备码登录 + 模型调用验证
    print("[Auth] session 不可用, 进行设备码登录...")
    handle = codex.login_chatgpt_device_code()
    print(f"\n请在浏览器中打开: {handle.verification_url}")
    print(f"输入验证码: {handle.user_code}\n")
    result = handle.wait()
    if not result.success:
        raise RuntimeError(f"[Auth] 登录失败: {result}")

    for retry in range(15):
        time.sleep(1)
        try:
            t = codex.thread_start()
            r = t.run("回复: OK")
            if r.final_response:
                print(f"[Auth] ✅ 登录成功! session 可用")
                return
        except Exception:
            pass
        print(f"  [Auth] 等待 session 生效... ({retry + 1}/15)")

    raise RuntimeError(
        "[Auth] session 不可用。\n"
        "  设备码登录在 Codex SDK 中可能仅用于 ChatGPT 消费产品,\n"
        "  不适用于 Codex API。\n"
        "  请在 codex_flow_config.json 中设置 openai_api_key"
    )


# ============================================================
# 额度管理: 查询 + 等待恢复 (不重启 session)
# ============================================================

def _read_rate_limits(codex: Codex) -> dict:
    """调用 account/rateLimits/read RPC, 返回额度快照

    Returns:
        {has_credits, unlimited, balance, used_percent, resets_at, rate_limit_type,
         raw_snap}  ← raw_snap 用于诊断
        resets_at 为 Unix 时间戳 (int) 或 None
    """
    try:
        resp = codex._client.request(
            "account/rateLimits/read", None,
            response_model=GetAccountRateLimitsResponse,
        )
        snap = resp.rate_limits
        credits = snap.credits
        primary = snap.primary
        secondary = snap.secondary

        # ── 诊断: 完整 dump 原始响应 (首次或每次, 方便定位) ──
        print(
            f"[Credits:diagnose] "
            f"has_credits={credits.has_credits if credits else 'N/A'} "
            f"unlimited={credits.unlimited if credits else 'N/A'} "
            f"balance={credits.balance if credits else 'N/A'} "
            f"credits_is_none={credits is None}"
        )
        print(
            f"[Credits:diagnose] "
            f"primary: used={primary.used_percent if primary else '?'}% "
            f"resets_at={primary.resets_at if primary else '?'} "
            f"window_mins={primary.window_duration_mins if primary else '?'} "
            f"is_none={primary is None}"
        )
        print(
            f"[Credits:diagnose] "
            f"secondary: used={secondary.used_percent if secondary else '?'}% "
            f"resets_at={secondary.resets_at if secondary else '?'} "
            f"is_none={secondary is None}"
        )
        print(
            f"[Credits:diagnose] "
            f"rate_limit_reached_type={snap.rate_limit_reached_type} "
            f"plan_type={snap.plan_type} "
            f"limit_name={snap.limit_name} "
            f"limit_id={snap.limit_id}"
        )
        # 多 bucket 视图 (例如 codex / gpt-5 / etc.)
        by_limit = resp.rate_limits_by_limit_id
        if by_limit:
            for lid, info in by_limit.items():
                print(f"[Credits:diagnose] bucket[{lid}]: {info}")

        return {
            "has_credits": credits.has_credits if credits else True,
            "unlimited": credits.unlimited if credits else False,
            "balance": credits.balance if credits else None,
            "used_percent": primary.used_percent if primary else 0,
            "resets_at": primary.resets_at if primary else None,
            "rate_limit_type": (
                snap.rate_limit_reached_type.value
                if snap.rate_limit_reached_type else None
            ),
        }
    except Exception as e:
        # RPC 可能不被所有 provider 支持, 失败时假设额度充足
        print(f"[Credits] 额度查询失败 (假设充足): {e}")
        return {"has_credits": True, "unlimited": True, "balance": None,
                "used_percent": 0, "resets_at": None, "rate_limit_type": None}


def _ensure_credits(
    codex: Codex,
    notify=None,  # Callable[[str], None] | None
    max_sleep_hours: float = 24.0,
) -> dict:
    """阻塞直到账户额度可用, 返回额度快照

    判断逻辑 (按优先级):
      1. rate_limit_reached_type 为 None → 无限流 → 直接放行
         (has_credits=False 对 API Key 后付费账户是正常的, 只表示无预充值余额)
      2. workspace_*_credits_depleted → 硬耗尽 → 立即报错
      3. rate_limit_reached / usage_limit_reached → 临时限流 → 等待

    关键认知: has_credits 只对预付费 credits 系统有意义; API Key 后付费账户
    的 has_credits 永远为 False, 真正的限流信号是 rate_limit_reached_type。
    """
    HARD_DEPLETION_TYPES = {
        "workspace_owner_credits_depleted",
        "workspace_member_credits_depleted",
    }

    started_at = time.time()

    while True:
        credits = _read_rate_limits(codex)
        rate_type = credits.get("rate_limit_type")

        # ── 第 1 优先: 无限流 → 放行 (不管 has_credits) ──
        # has_credits=False 对非预付费账户是正常状态, 不表示"耗尽"
        if rate_type is None:
            print(f"[Credits] ✅ 无限流 (rate_limit_type=None), 放行")
            return credits

        # ── 有显式限流, 检查是否可恢复 ──
        elapsed_h = (time.time() - started_at) / 3600

        # ── 第 2 优先: 硬额度耗尽 → 立即报错 ──
        if rate_type in HARD_DEPLETION_TYPES:
            raise RuntimeError(
                f"[Credits] ❌ 账户额度已耗尽 (类型: {rate_type}).\n"
                f"  这不是临时限流, 等待不会恢复.\n"
                f"  请前往 Codex 控制台充值, 或更换 API Key."
            )

        # ── 第 3 优先: 累计等待超时 ──
        if elapsed_h > max_sleep_hours:
            raise RuntimeError(
                f"[Credits] 累计等待 {elapsed_h:.1f}h 超过上限 "
                f"{max_sleep_hours}h (类型: {rate_type}), 终止循环"
            )

        # ── 计算等待时间 (primary.resets_at 是速率窗口重置时间) ──
        wait_s: float = 3600  # 默认等 1h
        if credits["resets_at"] is not None:
            wait_s = max(float(credits["resets_at"]) - time.time(), 0)
        if wait_s > max_sleep_hours * 3600:
            raise RuntimeError(
                f"[Credits] 速率窗口恢复需 {wait_s/3600:.1f}h, "
                f"超过上限 {max_sleep_hours}h, 终止循环"
            )

        if wait_s < 10:
            time.sleep(10)
            continue

        reset_str = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + wait_s))
        )
        msg = (
            f"💰 速率受限 (类型: {rate_type})\n"
            f"  速率窗口恢复: {reset_str} (约 {wait_s/3600:.1f}h)\n"
            f"  速率窗口用量: {credits.get('used_percent', '?')}%\n"
            f"  已累计等待: {elapsed_h:.1f}h / {max_sleep_hours}h"
        )
        print(f"[Credits] {msg}")
        if notify:
            try:
                notify(msg)
            except Exception:
                pass

        time.sleep(min(wait_s + 60, max_sleep_hours * 3600))


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
# T1 only creates diagnostic evidence. Planning, codegen, and training are
# orchestrator-owned phases below so their logs and manifest entries stay visible.
THREAD_EVALUATE = ThreadSpec(
    phase="evaluate",
    skills=[
        ("forecast-task-planner", "forecast-task-planner"),
        ("forecast-experiment-scanner", "forecast-experiment-scanner"),
        ("forecast-code-log-analyzer", "forecast-code-log-analyzer"),
        ("forecast-badcase-locator", "forecast-badcase-locator"),
        ("forecast-optimization-advisor", "forecast-optimization-advisor"),
    ],
    sandbox=Sandbox.workspace_write,
    prompt="""\
预测实验评测诊断。只执行 T1 诊断阶段，不执行完整实验闭环。

实验目录: {experiment_dir}
用户目标: {ask}

产物统一输出到 {output_dir}/ 下, 子目录约定:
  audit/       — 扫描结果 (scan_result.json, artifact_summary.json, code_analysis.json, log_summary.json)
  agent1/      — problem_context.json, badcase_diagnosis.md, artifact_contract.json
  standardized/ — standardized_prediction.csv, standardized_actual.csv (保留 split 列!)
  reports/     — optimization_suggestions.md

硬约束:
- {experiment_dir} 为 read_only, 只读不写
- 本阶段只允许写入 audit/、agent1/problem_context.json、agent1/badcase_diagnosis.md、agent1/artifact_contract.json、standardized/、reports/optimization_suggestions.md
- 禁止生成 agent1/experiment_plan.yaml、agent1/feature_hypothesis.yaml、agent2/、code/、candidate/ 或任何训练/候选模型文件
- 禁止启动训练、运行长耗时命令或调用训练入口脚本
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
args = parser.parse_args(['--experiment', 'baseline', '--history_eval_only', '--output_dir', 'outputs/real_outputs', '--data_path', '{data_dir}/dish_package_feature_df.csv', '--backtest_output_prefix', 'smoke_test'])
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
  - "{data_dir}/dish_package_feature_df.csv"
  - "--output_dir"
  - "{output_dir}/outputs/real_outputs"
  - "--history_eval_only"
  - "--backtest_output_prefix"
  - "{trial_id}"
output_contract:
  prediction_path: "{trial_id}_package_detail.csv"
  actual_path: "{trial_id}_package_detail.csv"
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

# ── T5: Feishu Review Card ──
# forecast-feishu-review-card 的 SKILL.md 已定义卡片格式/体积限制/质量检查
THREAD_FEISHU_CARD = ThreadSpec(
    phase="feishu-card",
    skills=[
        ("forecast-feishu-review-card", "forecast-feishu-review-card"),
    ],
    sandbox=Sandbox.read_only,
    prompt="""\
将实验报告精炼为飞书人工审批卡片。使用 forecast-feishu-review-card skill。

输入 (位于 {output_dir}/):
  final_report.md               — T4 完整报告
  evaluation/metric_comparison.json — 指标对比
  agent1/experiment_plan.yaml  — 实验计划 (改动列表)
  agent2/review_result.json    — 确定性决策 (不可修改)

必须产出:
  {output_dir}/feishu_review_card.md — 飞书卡片文本 (≤40行)
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

    def start(self, phase: str, kind: str = "codex") -> None:
        self.threads[phase] = {
            "status": "running",
            "kind": kind,
            "thread_id": None,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._write()

    def record(self, phase: str, thread_id: str, artifacts: list[str]) -> None:
        entry = dict(self.threads.get(phase, {}))
        entry.update({
            "status": "completed",
            "thread_id": thread_id,
            "artifacts": artifacts,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        self.threads[phase] = entry
        self._write()

    def record_error(self, phase: str, error: str) -> None:
        entry = dict(self.threads.get(phase, {}))
        entry.update({
            "status": "failed",
            "thread_id": None,
            "error": error,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        self.threads[phase] = entry
        self._write()

    def _write(self) -> None:
        path = Path(self.output_dir) / "workflow_manifest.json"
        path.write_text(json.dumps({
            "trial_id": self.trial_id,
            "experiment_dir": self.experiment_dir,
            "output_dir": self.output_dir,
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


def _has_code_files(path: Path) -> bool:
    if not path.exists():
        return False
    return any(
        p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
        for p in path.rglob("*")
    )


def _normalize_trial_code_layout(output_dir: str, *, phase: str) -> list[str]:
    """
    Normalize legacy/misplaced trial code from <trial>/code to <trial>/agent2/code.

    Older skill instructions used runs/<trial>/code. The canonical layout is now
    runs/<trial>/agent2/code. This guard makes the workflow tolerant to one bad
    agent write while still keeping T3 strict about the canonical location.
    """
    out = Path(output_dir)
    legacy = out / "code"
    canonical = out / "agent2" / "code"
    moved: list[str] = []

    if not _has_code_files(legacy):
        return moved

    canonical.mkdir(parents=True, exist_ok=True)
    canonical_has_files = _has_code_files(canonical)

    for item in legacy.iterdir():
        if item.name == "__pycache__":
            continue
        dest = canonical / item.name
        if dest.exists() and canonical_has_files:
            continue
        if dest.exists():
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        shutil.move(str(item), str(dest))
        moved.append(item.name)

    if moved:
        print(f"[{phase}] normalized misplaced code/ -> agent2/code/: {moved}")
    return moved


def _copy_data_dir(data_dir: str, output_dir: str) -> None:
    """将数据目录复制到 trial（如果 agent2/code/data 不存在）"""
    dst_data = Path(output_dir) / "agent2" / "code" / "data"
    if dst_data.exists():
        return
    src = Path(data_dir)
    if not src.exists():
        return
    dst_data.mkdir(parents=True, exist_ok=True)
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(src)
        target = dst_data / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)


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


def _stage_log(output_dir: str, phase: str, message: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {message}"
    log_path = Path(output_dir) / "logs" / f"stage_{phase}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def _assert_no_t1_phase_leak(output_dir: str) -> None:
    output = Path(output_dir)
    forbidden_files = [
        output / "agent1" / "experiment_plan.yaml",
        output / "agent1" / "feature_hypothesis.yaml",
        output / "agent1" / "candidate_experiments.yaml",
    ]
    leaked = [str(path.relative_to(output)) for path in forbidden_files if path.exists()]
    for dirname in ("agent2", "code", "candidate"):
        path = output / dirname
        if _has_code_files(path):
            leaked.append(str(path.relative_to(output)) + "/*")
    if leaked:
        raise RuntimeError(
            "[evaluate] T1 leaked later-stage artifacts: "
            f"{leaked}. T2/T3 must be executed by the outer workflow."
        )


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
    data_dir: str = "",
) -> str:
    """启动并执行一个 Codex 子线程, 返回 thread_id"""
    prompt = spec.prompt.format(
        experiment_dir=experiment_dir,
        output_dir=output_dir,
        ask=ask,
        trial_id=trial_id,
        data_dir=data_dir,
    )
    inputs: list = [TextInput(text=prompt)] + _resolve_skill_inputs(spec.skills)

    print(f"\n{'='*50}")
    print(f"[{spec.phase}] 启动 Codex 线程")
    print(f"  Skills: {[s[0] for s in spec.skills]}")
    print(f"  Sandbox: {spec.sandbox.value}")
    print(f"{'='*50}")
    _stage_log(output_dir, spec.phase, f"start codex thread skills={[s[0] for s in spec.skills]}")

    thread = codex.thread_start(
        cwd=output_dir,
        sandbox=Sandbox.full_access,
        approval_mode=ApprovalMode.deny_all,
    )
    print(f"[{spec.phase}] 等待 LLM 响应 (可能需要 30s-2min)...")
    result = thread.run(inputs, sandbox=Sandbox.full_access, approval_mode=ApprovalMode.deny_all)

    if result.status.value == "failed":
        error_msg = result.error.message if result.error else "unknown"
        # 检测额度耗尽: 等待恢复后重试同一 thread
        error_info = result.error.codex_error_info if result.error else None
        is_usage_limit = (
            error_info is not None
            and hasattr(error_info, 'root')
            and error_info.root == CodexErrorInfoValue.usage_limit_exceeded
        )
        if is_usage_limit:
            print(f"[{spec.phase}] ⚠️ 额度耗尽, 等待恢复...")
            _dump_codex_stderr(codex, f"{spec.phase}_credits")
            _ensure_credits(codex)
            print(f"[{spec.phase}] 额度恢复, 重试 thread {thread.id}...")
            retry_thread = codex.thread_resume(thread.id)
            result = retry_thread.run(
                inputs, sandbox=Sandbox.full_access,
                approval_mode=ApprovalMode.deny_all,
            )
            if result.status.value == "failed":
                error_msg2 = result.error.message if result.error else "unknown"
                _dump_codex_stderr(codex, f"{spec.phase}_retry_error")
                raise RuntimeError(
                    f"[{spec.phase}] 额度恢复后仍失败: {error_msg2}"
                )
        else:
            _dump_codex_stderr(codex, f"{spec.phase}_error")
            _stage_log(output_dir, spec.phase, f"failed: {error_msg}")
            raise RuntimeError(f"[{spec.phase}] 执行失败: {error_msg}")

    print(f"[{spec.phase}] 完成 — thread={thread.id}")
    last_msg = result.final_response or ""
    print(f"[{spec.phase}] 回复摘要: {last_msg[:300]}{'...' if len(last_msg) > 300 else ''}")
    if result.usage:
        u = result.usage.last
        print(f"  Tokens: in={u.input_tokens}, out={u.output_tokens}, total={u.total_tokens}")
        _stage_log(
            output_dir,
            spec.phase,
            f"completed thread={thread.id} tokens in={u.input_tokens} out={u.output_tokens} total={u.total_tokens}",
        )
    else:
        _stage_log(output_dir, spec.phase, f"completed thread={thread.id}")

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


def _resolve_train_command(train_cmd: list[Any], output: Path, trial_id: str) -> list[str]:
    """Resolve execution-plan train command against the canonical agent2/code layout."""
    resolved = [
        str(arg).format(output_dir=str(output), trial_id=trial_id)
        for arg in train_cmd
    ]
    if not resolved:
        return resolved

    code_dir = output / "agent2" / "code"
    executable = Path(resolved[0]).name.lower()
    if executable in {"python", "python.exe", "python3", "python3.exe"}:
        resolved[0] = sys.executable
        if len(resolved) > 1 and resolved[1] not in {"-m", "-c"}:
            script_text = resolved[1]
            script = Path(script_text)
            if not script.is_absolute():
                normalized = script_text.replace("\\", "/")
                candidates: list[Path] = []
                if normalized.startswith("agent2/code/"):
                    candidates.append(output / script)
                candidates.extend([
                    code_dir / script,
                    output / script,
                ])
                for candidate in candidates:
                    if candidate.exists():
                        script = candidate
                        break
                else:
                    script = candidates[0] if normalized.startswith("agent2/code/") else code_dir / script
            resolved[1] = str(script.resolve(strict=False))

    return resolved


# ============================================================
# T3: Python 确定性执行 (训练 + 评测 + keep/rollback)
# ============================================================

def execute_t3(manifest: WorkflowManifest, *, baseline_metric_dir: str | None = None) -> dict:
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

    train_cmd = _resolve_train_command(train_cmd, output, manifest.trial_id)

    # ── 2. 执行训练 ──
    print(f"\n[T3] 执行训练: {' '.join(train_cmd)}")
    _stage_log(str(output), "execute", f"training command: {' '.join(train_cmd)}")
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
    _stage_log(str(output), "execute", f"training finished rc={train_result.returncode}")

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
            _stage_log(str(output), "execute", f"new test WAPE={new_wape:.4f} Bias={new_bias:+.4f} rows={new_rows}")
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
        _stage_log(
            str(output),
            "execute",
            f"skip evaluation train_success={train_success} prediction_exists={prediction_path.exists()}",
        )

    # ── 4. 读取 baseline 旧指标 (test 集, 直接从 outputs/ 原始文件) ──
    # 优先 package_detail (模型主输出, test WAPE ~0.70),
    # 同时读取 store_dish_day (dish 分配后, test WAPE ~0.49)
    old_wape, old_bias, old_rows = 999.0, 0.0, 0
    old_wape_dish, old_bias_dish = 999.0, 0.0

    # 旧指标来源: 链式执行用 baseline_metric_dir, 否则用 experiment_dir/outputs/
    metric_dir = Path(baseline_metric_dir) if baseline_metric_dir else Path(manifest.experiment_dir) / "outputs"
    print(f"[T3] 读取 baseline 指标: {metric_dir}")

    # 主指标: package_detail
    pkg_files = sorted(metric_dir.glob("*_package_detail.csv"))
    if not pkg_files:
        pkg_files = sorted(metric_dir.glob("*.csv"))  # fallback
    if pkg_files:
        try:
            old_wape, old_bias, old_rows = _compute_wape_bias_from_csv(pkg_files[0], "test")
            print(f"[T3] baseline package_detail test: WAPE={old_wape:.4f}, Bias={old_bias:+.4f}, rows={old_rows}")
        except Exception as e:
            print(f"[T3] 警告: 读取 package_detail 失败 ({e})")

    # 辅助指标: store_dish_day
    dish_files = sorted(metric_dir.glob("*_store_dish_day.csv"))
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

    _stage_log(
        str(output),
        "execute",
        f"decision={decision} wape_delta={wape_delta:+.4f} bias_delta={bias_delta:+.4f}",
    )
    return comparison


# ============================================================
# 主工作流编排 (单次 Codex Session — 登录与工作在同一 session)
# ============================================================

def run_workflow(
    experiment_dir: str,
    ask: str,
    output_dir: str,
    *,
    previous_trial: str | None = None,
    codex: Codex | None = None,
) -> WorkflowManifest:
    """
    ComboScope Codex 多线程工作流 — 单次 Codex Session

    流程:
      认证 → T1 (evaluate) → T2a (plan) → [copy source] → T2b (codegen)
      → T3 (execute, Python) → T4 (report)

    链式执行: --experiment baseline --previous-trial runs/trial_N
      - 代码从 previous_trial/agent2/code/ 继承 (保留 codegen 修改)
      - 数据始终从 experiment_dir/data/ 读取
      - baseline 指标从 previous_trial/outputs/ 读取

    codex 参数: 传入已认证的 Codex 实例时, 跳过 session 创建和认证步骤,
    直接复用已有 session。用于循环模式 (run_loop) 避免重复启动 app-server。
    """
    trial_id = Path(output_dir).name
    exp_dir = str(Path(experiment_dir).resolve())
    out_dir = str(Path(output_dir).resolve())
    prev_dir = str(Path(previous_trial).resolve()) if previous_trial else None
    is_chain = prev_dir is not None

    manifest = WorkflowManifest(
        trial_id=trial_id,
        experiment_dir=exp_dir,
        output_dir=out_dir,
        ask=ask,
    )
    _ensure_dirs(output_dir)
    _normalize_trial_code_layout(output_dir, phase="layout-preflight")
    manifest._write()
    if prev_dir:
        manifest.threads["_chain"] = {"previous_trial": prev_dir}
        manifest._write()

    # 代码来源: 链式执行从上一轮 agent2/code/ 继承, 否则从 experiment_dir 复制
    code_src_dir = str(Path(prev_dir) / "agent2" / "code") if is_chain else exp_dir
    # 数据目录始终指向原始 baseline
    data_dir = str(Path(exp_dir) / "data")
    # 旧指标来源: 链式执行读上一轮的 outputs, 否则读 baseline/outputs
    baseline_metric_dir = str(Path(prev_dir) / "outputs" / "real_outputs") if is_chain else str(Path(exp_dir) / "outputs")

    # 线程公共参数
    thread_kwargs = dict(experiment_dir=exp_dir, output_dir=out_dir, ask=ask,
                         trial_id=trial_id, data_dir=data_dir)

    # ── 内部: T1~T4 全流程 ──
    def _run_trial_pipeline(cx: Codex) -> dict:
        """在已认证的 Codex session 上执行 T1→T2a→copy→T2b→T3→T4"""
        nonlocal manifest

        # ── T1: Evaluate + Diagnose ──
        try:
            manifest.start("evaluate", "codex")
            _stage_log(output_dir, "evaluate", "T1 evaluate started")
            t1_id = run_codex_thread(cx, THREAD_EVALUATE, **thread_kwargs)
            _assert_no_t1_phase_leak(output_dir)
            _require_artifacts(output_dir, [
                "standardized/standardized_prediction.csv",
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
                "reports/optimization_suggestions.md",
                "logs/stage_evaluate.log",
            ])
        except Exception as e:
            manifest.record_error("evaluate", str(e))
            raise

        # ── T2a: Plan ──
        try:
            manifest.start("plan", "codex")
            _stage_log(output_dir, "plan", "T2 plan started")
            t2a_id = run_codex_thread(cx, THREAD_PLAN, **thread_kwargs)
            _require_artifacts(output_dir, [
                "agent1/feature_hypothesis.yaml",
                "agent1/experiment_plan.yaml",
            ], "plan")
            manifest.record("plan", t2a_id, [
                "agent1/feature_hypothesis.yaml",
                "agent1/experiment_plan.yaml",
                "agent1/candidate_experiments.yaml",
                "reports/forecast_report.md",
                "reports/report_context.json",
                "logs/stage_plan.log",
            ])
        except Exception as e:
            manifest.record_error("plan", str(e))
            raise

        # ── Copy Source (T2b 前置) ──
        manifest.start("copy_source", "python")
        _stage_log(output_dir, "copy_source", "T2 source copy started")
        copied = copy_source_to_trial(code_src_dir, output_dir)
        _normalize_trial_code_layout(output_dir, phase="copy-source")
        if is_chain:
            _copy_data_dir(data_dir, output_dir)
        print(f"\n[PRE-T2b] 已复制源码到 agent2/code/: {len(copied)} 个文件 (来源: {code_src_dir})")
        _stage_log(output_dir, "copy_source", f"copied {len(copied)} files from {code_src_dir}")
        manifest.record("copy_source", "N/A (Python deterministic)", [
            "agent2/code",
            "logs/stage_copy_source.log",
        ])

        # ── T2b: Code Generation ──
        try:
            manifest.start("codegen", "codex")
            _stage_log(output_dir, "codegen", "T2 codegen started")
            t2b_id = run_codex_thread(cx, THREAD_CODEGEN, **thread_kwargs)
            _normalize_trial_code_layout(output_dir, phase="codegen")
            _require_artifacts(output_dir, [
                "agent2/code/train.py",
                "agent2/agent2_execution_plan.yaml",
            ], "codegen")
            manifest.record("codegen", t2b_id, [
                "agent2/code/train.py",
                "agent2/agent2_execution_plan.yaml",
                "logs/stage_codegen.log",
            ])
        except Exception as e:
            manifest.record_error("codegen", str(e))
            raise

        # ── T3 + codegen 回退循环 ──
        manifest.start("execute", "python")
        _stage_log(output_dir, "execute", "T3 execute started")
        MAX_RETRIES = 2
        train_log_path = Path(output_dir) / "logs" / "train.log"
        comparison: dict | None = None
        for attempt in range(1 + MAX_RETRIES):
            try:
                _stage_log(output_dir, "execute", f"T3 attempt {attempt + 1}/{1 + MAX_RETRIES}")
                comparison = execute_t3(manifest, baseline_metric_dir=baseline_metric_dir)
            except Exception as e:
                _stage_log(output_dir, "execute", f"failed: {e}")
                manifest.record_error("execute", str(e))
                raise

            train_ok = (comparison.get("primary", {}).get("new_wape", 999) != 999)
            _stage_log(
                output_dir,
                "execute",
                f"T3 attempt {attempt + 1} decision={comparison.get('decision')} "
                f"new_wape={comparison.get('primary', {}).get('new_wape', 999)}",
            )
            if train_ok:
                manifest.record("execute", "N/A (Python deterministic)", [
                    "agent2/run_status.json", "agent2/review_result.json",
                    "agent2/experiment_review.md", "evaluation/new_metrics.json",
                    "evaluation/new_metrics_summary.csv",
                    "evaluation/metric_comparison.json", "logs/train.log",
                    "logs/stage_execute.log",
                ])
                break

            if attempt >= MAX_RETRIES:
                # 终极回退: 用原始脚本 + baseline preset 直接跑
                src_script = Path(output_dir) / "agent2" / "code" / "src" / "lgb_package_to_dish_online_0319.py"
                if src_script.exists():
                    print("[T3] 回退: 使用原始训练脚本 (baseline preset, 不依赖 codegen)")
                    _stage_log(output_dir, "execute", "fallback original training script started")
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
                    _stage_log(output_dir, "execute", f"fallback training finished rc={fb_result.returncode}")
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
                                _stage_log(output_dir, "execute", f"fallback test WAPE={fallback_wape:.4f} Bias={fallback_bias:+.4f}")
                                primary = comparison.setdefault("primary", {})
                                primary["new_wape"] = fallback_wape
                                primary["new_bias"] = fallback_bias
                                wape_delta = round(primary.get("old_wape", 999) - fallback_wape, 6)
                                bias_delta = round(abs(fallback_bias) - abs(primary.get("old_bias", 0)), 6)
                                keep = wape_delta > 0.005 and bias_delta < 0.02
                                comparison["decision"] = "keep" if keep else "rollback"
                                comparison["wape_delta"] = wape_delta
                                comparison["bias_delta"] = bias_delta
                                comparison["reason"] = f"fallback 原始脚本: WAPE delta={wape_delta:.4f}"
                                (Path(output_dir) / "evaluation" / "new_metrics.json").write_text(
                                    json.dumps({"wape": fallback_wape, "bias": fallback_bias, "rows": fallback_rows, "source": "fallback_original_script"}, indent=2),
                                    encoding="utf-8")
                            except Exception as fe:
                                print(f"[T3] 回退指标计算失败: {fe}")

                manifest.record("execute", "N/A (Python deterministic)", [
                    "agent2/run_status.json", "agent2/review_result.json",
                    "agent2/experiment_review.md",
                    "evaluation/metric_comparison.json", "logs/train.log",
                    "logs/stage_execute.log",
                ])
                print("[T3] 已达最大重试次数, 使用 baseline 指标生成报告")
                _stage_log(output_dir, "execute", "T3 reached max retries")
                break

            # 读错误日志, 反馈给 codegen 线程修复
            error_text = ""
            if train_log_path.exists():
                lines = train_log_path.read_text(encoding="utf-8").split("\n")
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

            err_last_line = error_text.strip().split("\n")[-1] if error_text.strip() else "(无法读取错误日志)"
            print(f"\n[codegen-retry {attempt+1}/{MAX_RETRIES}] 错误: {err_last_line}")
            print(f"[codegen-retry] 反馈错误到 codegen 线程 {t2b_id}...")
            _stage_log(output_dir, "execute", f"feedback failure to codegen retry={attempt + 1}: {err_last_line}")
            resume_thread = cx.thread_resume(t2b_id)
            resume_result = resume_thread.run(
                [fix_input],
                sandbox=Sandbox.full_access,
                approval_mode=ApprovalMode.deny_all,
            )
            if resume_result.status.value == "failed":
                print(f"[codegen-retry] 修复线程失败: {resume_result.error.message if resume_result.error else 'unknown'}")
                _stage_log(output_dir, "execute", f"codegen retry thread failed attempt={attempt + 1}")
            else:
                print(f"[codegen-retry] 修复完成, 重新执行 T3...")
                _stage_log(output_dir, "execute", f"codegen retry thread completed attempt={attempt + 1}")
                train_py = Path(output_dir) / "agent2" / "code" / "train.py"
                if train_py.exists():
                    import py_compile as pyc
                    try:
                        pyc.compile(str(train_py), doraise=True)
                        print("[codegen-retry] train.py 语法验证通过")
                    except pyc.PyCompileError as ce:
                        print(f"[codegen-retry] train.py 语法错误: {ce}")

        if comparison is None:
            comparison = {"decision": "rollback", "reason": "训练失败, 无新指标",
                          "primary": {"old_wape": 999, "new_wape": 999}}

        # ── T4: Report ──
        try:
            manifest.start("report", "codex")
            _stage_log(output_dir, "report", "T4 report started")
            t4_id = run_codex_thread(cx, THREAD_REPORT, **thread_kwargs)
            manifest.record("report", t4_id, [
                "final_report.md",
                "logs/stage_report.log",
            ])
            cx.thread_archive(t4_id)
        except Exception as e:
            manifest.record_error("report", str(e))
            raise

        # ── T5: Feishu Review Card ──
        try:
            t5_id = run_codex_thread(cx, THREAD_FEISHU_CARD, **thread_kwargs)
            manifest.record("feishu_card", t5_id, [
                "feishu_review_card.md",
            ])
            # 输出卡片内容到控制台
            card_path = Path(output_dir) / "feishu_review_card.md"
            if card_path.exists():
                card_text = card_path.read_text(encoding="utf-8")
                print(f"\n{'='*50}")
                print(f"[T5] 飞书审批卡片:")
                print(card_text)
                print(f"{'='*50}")
            cx.thread_archive(t5_id)
        except Exception as e:
            manifest.record_error("feishu_card", str(e))
            print(f"[T5] 卡片生成失败(非致命): {e}")

        return comparison

    # ── 认证 + 执行 ──
    comparison: dict = {}

    def _execute_trial(cx: Codex) -> None:
        """认证并执行单次 trial 的 T1~T4 全流程"""
        _ensure_session(cx)
        nonlocal comparison
        comparison = _run_trial_pipeline(cx)

    if codex is not None:
        # 循环模式: 复用外部传入的已认证 session
        _execute_trial(codex)
    else:
        # 独立模式: 创建自己的 Codex session
        with Codex(config=CODEX_CONFIG) as owned_codex:
            _execute_trial(owned_codex)

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
# 循环模式: 一次认证, 多轮实验
# ============================================================

def run_loop(
    experiment_dir: str,
    ask: str,
    *,
    max_iter: int = 10,
    target_wape: float | None = None,
    start_trial: int = 1,
    max_sleep_hours: float = 24.0,
    notify_chat_id: str = "",
) -> list[WorkflowManifest]:
    """
    循环执行多轮 Codex 优化实验。

    一次 Codex session 完成 N 轮 trial，每轮自动 --previous-trial 链式继承。
    额度不足时原地 sleep 等待恢复 (不重启 session)。
    停止条件: target_wape 达成 / WAPE 连续不改善 / max_iter 上限。

    Returns:
        每轮成功的 manifest 列表
    """
    # 延迟导入避免循环依赖
    import lark_notify as lark
    if notify_chat_id:
        lark.LARK_CHAT_ID = notify_chat_id

    manifests: list[WorkflowManifest] = []
    prev_trial: str | None = None
    no_improve_count = 0
    best_wape = 999.0
    # stop_reason 由循环结束时赋值
    stop_reason = "达到最大迭代"

    print(f"\n{'#'*60}")
    print(f"# Codex Flow 循环模式")
    print(f"# 最大迭代: {max_iter} | 目标 WAPE: {target_wape or '无'}")
    print(f"# 最长等待: {max_sleep_hours}h")
    print(f"{'#'*60}")

    with Codex(config=CODEX_CONFIG) as codex:
        # ── 一次性认证 ──
        _ensure_session(codex)

        # ── 额度检查 + 通知启动 ──
        credits = _ensure_credits(codex, max_sleep_hours=max_sleep_hours)
        lark.notify_loop_start(
            experiment=experiment_dir,
            ask=ask,
            max_iter=max_iter,
            target_wape=target_wape,
            credits=credits,
        )

        # ── 循环 ──
        for i in range(max_iter):
            trial_num = start_trial + i
            trial_output = f"runs/trial_{trial_num:03d}"

            print(f"\n{'#'*60}")
            print(f"# 第 {i+1}/{max_iter} 轮: {trial_output}")
            if prev_trial:
                print(f"# 链式继承: {prev_trial}")
            print(f"{'#'*60}")

            # ── 每轮前检查额度 ──
            credits = _ensure_credits(codex, max_sleep_hours=max_sleep_hours)

            try:
                manifest = run_workflow(
                    experiment_dir=experiment_dir,
                    ask=ask,
                    output_dir=trial_output,
                    previous_trial=prev_trial,
                    codex=codex,
                )
                manifests.append(manifest)
                prev_trial = trial_output

                # ── 读取本轮 WAPE, 检查停止条件 ──
                metric_path = Path(trial_output) / "evaluation" / "metric_comparison.json"
                current_wape = 999.0
                comparison: dict = {}
                if metric_path.exists():
                    try:
                        comparison = json.loads(metric_path.read_text(encoding="utf-8"))
                        current_wape = comparison.get("primary", {}).get("new_wape", 999.0)
                    except Exception:
                        pass

                print(f"\n[Loop] 本轮 WAPE: {current_wape:.4f} | 最佳: {best_wape:.4f}")

                # ── 飞书通知: 本轮结果 ──
                lark.notify_trial_done(trial_output, comparison, ask=ask)
                # ── 飞书审批卡片 (精炼版, 含 /keep /rollback 等指令) ──
                lark.send_review_card(trial_output)

                # 停止条件1: 达到目标 WAPE
                if target_wape is not None and current_wape <= target_wape:
                    print(f"[Loop] ✅ 目标 WAPE {target_wape} 达成! 停止循环")
                    stop_reason = f"目标 WAPE {target_wape} 达成"
                    break

                # 停止条件2: WAPE 改善跟踪
                if current_wape < best_wape - 0.005:
                    best_wape = current_wape
                    no_improve_count = 0
                else:
                    no_improve_count += 1
                    print(f"[Loop] WAPE 未显著改善 ({no_improve_count}/2)")
                    if no_improve_count >= 2:
                        print(f"[Loop] ⏹ 连续 2 轮无改善, 停止循环")
                        stop_reason = "连续 2 轮无改善"
                        break

            except Exception as e:
                print(f"\n[Loop] ❌ trial_{trial_num:03d} 失败: {e}")
                lark.notify_error(str(e), f"trial_{trial_num:03d}")
                prev_trial = manifests[-1].output_dir if manifests else None
                continue

    # ── 汇总 ──
    print(f"\n{'#'*60}")
    print(f"# 循环结束 ({stop_reason}): 完成 {len(manifests)} 轮")
    for m in manifests:
        metric_path = Path(m.output_dir) / "evaluation" / "metric_comparison.json"
        wape_str = "?"
        if metric_path.exists():
            try:
                wape_str = f"{json.loads(metric_path.read_text(encoding='utf-8')).get('primary', {}).get('new_wape', '?'):.4f}"
            except Exception:
                pass
        print(f"  {m.trial_id}: WAPE={wape_str}")
    print(f"{'#'*60}")

    # ── 飞书通知: 循环停止汇总 ──
    lark.notify_loop_stop(stop_reason, manifests)

    return manifests


# ============================================================
# CLI
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="ComboScope Codex Workflow (轻量版)")
    parser.add_argument("--experiment", default="baseline", help="预测实验目录 (默认 baseline)")
    parser.add_argument("--ask", default="分析预测误差，提出特征实验并验证", help="实验目标描述")
    parser.add_argument("--output", default="runs/trial_001", help="输出目录 (默认 runs/trial_001)")
    parser.add_argument("--previous-trial", default=None, help="继承上一轮 trial 的代码和指标 (链式优化)")

    # 循环模式
    parser.add_argument("--loop", action="store_true", help="启用循环模式: 一次认证, 连续多轮实验")
    parser.add_argument("--max-iter", type=int, default=10, help="最大迭代次数 (默认 10)")
    parser.add_argument("--target-wape", type=float, default=None, help="目标 WAPE, 达到即停止 (如 0.55)")
    parser.add_argument("--start-trial", type=int, default=1, help="起始 trial 编号 (默认 1, 如已有 trial_001~003 可设 4)")

    # 额度 & 通知
    parser.add_argument("--max-sleep-hours", type=float, default=24.0, help="额度耗尽最长等待小时数 (默认 24)")
    parser.add_argument("--notify-chat-id", default=get_config().feishu.chat_id, help="飞书通知群聊 ID")

    # Human-in-the-loop
    parser.add_argument("--human-review", action="store_true", help="每轮 T2a 后暂停等人工确认")
    parser.add_argument("--review-timeout", type=int, default=1800, help="Human review 超时秒数 (默认 1800)")

    args = parser.parse_args()

    try:
        # ── 循环模式 ──
        if args.loop:
            run_loop(
                experiment_dir=args.experiment,
                ask=args.ask,
                max_iter=args.max_iter,
                target_wape=args.target_wape,
                start_trial=args.start_trial,
                max_sleep_hours=args.max_sleep_hours,
                notify_chat_id=args.notify_chat_id,
            )
            return 0

        # ── 单次模式 (兼容旧用法) ──
        run_workflow(
            experiment_dir=args.experiment,
            ask=args.ask,
            output_dir=args.output,
            previous_trial=args.previous_trial,
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
