#!/usr/bin/env python3
"""
codex_flow.py — ComboScope Codex 多线程工作流 (轻量版)

架构: 1 Codex Session → 5 阶段 (4 Codex Threads + 1 Python)

  T1: Evaluate + Diagnose (read_only) — 由 using-forecast skill 编排
  T2a: Plan (workspace_write) — 生成 experiment_plan + feature_hypothesis
  [Python: copy source → candidate/code/]
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
import fnmatch
import hashlib
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
from config import PROJECT_ROOT, build_codex_config, get_config, get_paths, override_data_primary
from codex_gateway import ensure_codex_gateway

# ============================================================
# 固定路径 — skills 目录 & 确定性执行脚本
# ============================================================

SKILLS_ROOT = Path(__file__).resolve().parent / "skills"

# T3 确定性执行依赖的脚本 (skill scripts, 固定路径)
EVAL_CALCULATE_SCRIPT = SKILLS_ROOT / "forecast-evaluation-analyzer" / "scripts" / "calculate_metrics.py"
MAX_T3_PREDICTION_BYTES = 1_000_000_000
T3_TRAIN_TIMEOUT_SECONDS = 3600
T3_METRIC_TIMEOUT_SECONDS = 300


class PredictionContractError(ValueError):
    """Raised when codegen produced an output that T3 should not evaluate."""


class HistoryEvalContractError(ValueError):
    """Raised when codegen violates the bounded --history_eval_only runtime path."""


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
        "  请设置 OPENAI_API_KEY 或 flow_config.yaml 的 auth.openai_api_key"
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

源码位置: {trial_code_dir}/  (只能修改此目录)
实验计划: {output_dir}/agent1/experiment_plan.yaml
特征假设: {output_dir}/agent1/feature_hypothesis.yaml
模型接入契约:
{model_contract}

必须产出 (验证通过后):
  {trial_code_dir}/train.py
  {output_dir}/agent2/agent2_execution_plan.yaml

=== 自验证流程 (MUST 按顺序执行) ===

1. 读取模型接入契约，选择已有入口或生成薄封装 train.py；优先复用 entrypoint_candidates 和 default_train_command。
2. 按 experiment_plan.changes 实现特征修改，修改范围只限 {trial_code_dir}/。
3. 语法检查: 执行 `python -m py_compile {trial_code_dir}/train.py`。
4. 冒烟检查: 至少验证 train.py 可导入或可执行帮助命令；如模型接入契约提供 entrypoint_candidates，需要验证所选入口存在且可导入或可被 train.py 调用。
5. 只有验证全部通过，才写入 agent2_execution_plan.yaml。

=== agent2_execution_plan.yaml 要求 ===

- train_command 必须是 list，并使用契约中的占位符路径；不要写死本机绝对路径。
- output_contract 必须来自模型接入契约；如需要覆盖预测文件名、真实值列、预测列、split 列或 split 过滤，只能写入明确字段。
- source_entrypoint 必须是相对 {trial_code_dir}/ 的路径。
- python_dependencies 只列真实存在的相对路径。

运行时约束:
- 训练命令必须在 T3 超时内完成。
- 输出文件必须满足 output_contract 中的 prediction_path、split_column、split_filter、actual_column/prediction_column 或候选列规则。
- 不要创建 data/、outputs/、logs/、__pycache__/ 到 {trial_code_dir}/ 下；数据通过 --data_path={data_path} 或契约指定参数读取。
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
  agent2/agent2_execution_plan.yaml, candidate/code/train.py, evaluation/metric_comparison.json,
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
    paths: dict[str, Any] = field(default_factory=dict)

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

    def record_degraded(self, phase: str, error: str, artifacts: list[str],
                        thread_id: str = "N/A (fallback deterministic)") -> None:
        entry = dict(self.threads.get(phase, {}))
        entry.update({
            "status": "degraded",
            "thread_id": thread_id,
            "error": error,
            "artifacts": artifacts,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        self.threads[phase] = entry
        self._write()

    def record_interrupted(self, phase: str, error: str,
                           thread_id: str | None = None) -> None:
        entry = dict(self.threads.get(phase, {}))
        attempts = int(entry.get("attempts", 0) or 0) + 1
        entry.update({
            "status": "interrupted",
            "thread_id": thread_id or entry.get("thread_id"),
            "error": error,
            "last_error": error,
            "attempts": attempts,
            "recoverable": True,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        self.threads[phase] = entry
        self._write()

    def is_completed(self, phase: str) -> bool:
        return self.threads.get(phase, {}).get("status") == "completed"

    def status(self, phase: str) -> str | None:
        return self.threads.get(phase, {}).get("status")

    def should_skip_phase(self, phase: str, *, resume: bool,
                          resume_from_phase: str | None = None) -> bool:
        if not resume:
            return False
        if resume_from_phase == phase:
            return False
        return self.is_completed(phase)

    def _write(self) -> None:
        path = Path(self.output_dir) / "workflow_manifest.json"
        path.write_text(json.dumps({
            "trial_id": self.trial_id,
            "experiment_dir": self.experiment_dir,
            "output_dir": self.output_dir,
            "ask": self.ask,
            "started_at": self.started_at,
            "paths": self.paths,
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
        m.paths = data.get("paths", {})
        return m


class StageInterrupted(RuntimeError):
    """Raised when a recoverable workflow stage needs manual continuation."""

    def __init__(self, trial_id: str, output_dir: str, phase: str, error: str):
        super().__init__(f"[{phase}] interrupted: {error}")
        self.trial_id = trial_id
        self.output_dir = output_dir
        self.phase = phase
        self.error = error


# ============================================================
# 源码复制 (T2b 前置)
# ============================================================

def _trial_code_dir(output_dir: str | Path) -> Path:
    return get_paths().trial_code_dir(output_dir)


def _legacy_code_dir(output_dir: str | Path) -> Path:
    return get_paths().legacy_trial_code_dir(output_dir)


def _existing_code_dir(output_dir: str | Path) -> Path:
    return get_paths().existing_trial_code_dir(output_dir)


def _trial_outputs_dir(output_dir: str | Path) -> Path:
    return get_paths().trial_outputs_dir(output_dir)


def _trial_inputs_dir(output_dir: str | Path) -> Path:
    return get_paths().trial_inputs_dir(output_dir)


def _rel_to_trial(output_dir: str | Path, path: str | Path) -> str:
    root = Path(output_dir).resolve()
    target = Path(path).resolve()
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        return target.as_posix()


def _resolve_git_repo_path(repo_root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def _matches_copy_spec(rel: str, specs: list[str]) -> bool:
    rel = rel.strip("/")
    if not specs:
        return True
    for raw in specs:
        spec = str(raw).strip().strip("/")
        if not spec:
            continue
        if spec.endswith("/**"):
            prefix = spec[:-3].strip("/")
            if rel == prefix or rel.startswith(prefix + "/"):
                return True
            continue
        if any(ch in spec for ch in "*?[") and fnmatch.fnmatch(rel, spec):
            return True
        if rel == spec or rel.startswith(spec + "/"):
            return True
    return False


def _configured_git_model_paths() -> dict[str, Any] | None:
    """Return active Git MCP model paths when they should seed trial code."""
    cfg = get_config().mcp.git
    if not cfg.enabled or cfg.scope != "baseline_model":
        return None

    repo_cfg = cfg.resolve_repo()
    repo = Path(repo_cfg.repo_path).expanduser()
    if not repo.is_absolute():
        repo = PROJECT_ROOT / repo
    repo = repo.resolve()
    model_cfg = getattr(repo_cfg, "model", None)
    model_root_value = getattr(model_cfg, "root", None) or getattr(repo_cfg, "baseline_dir", "baseline")
    model_root = _resolve_git_repo_path(repo, model_root_value)
    source = _resolve_git_repo_path(repo, getattr(repo_cfg, "source_dir", str(Path(model_root_value) / "src")))
    requirements = _resolve_git_repo_path(repo, getattr(repo_cfg, "requirements", str(Path(model_root_value) / "requirements.txt")))
    requirements_paths = [
        model_root / item
        for item in getattr(model_cfg, "requirements_paths", ["requirements.txt"])
    ]
    if model_root.exists():
        return {
            "repo": repo,
            "baseline": model_root,
            "model_root": model_root,
            "source": source,
            "requirements": requirements,
            "requirements_paths": requirements_paths,
            "copy_include": list(getattr(model_cfg, "copy_include", ["src/**", "requirements.txt", "train.py"])),
            "copy_exclude": list(getattr(model_cfg, "copy_exclude", ["__pycache__/**", "*.pyc", ".git/**", "data/**", "outputs/**", "logs/**"])),
            "entrypoint_candidates": list(getattr(model_cfg, "entrypoint_candidates", ["train.py", "src/train.py", "main.py"])),
            "default_train_command": list(getattr(model_cfg, "default_train_command", [])),
            "output_contract": dict(getattr(model_cfg, "output_contract", _default_output_contract())),
        }
    return None


def _configured_git_model_baseline_dir() -> Path | None:
    """Return the Git MCP worktree baseline directory when it should seed trial code."""
    paths = _configured_git_model_paths()
    return paths["baseline"] if paths else None


def _initial_code_source_dir(experiment_dir: str | Path) -> Path:
    """Prefer the synced Git MCP model worktree for source code, fallback to experiment_dir."""
    return _configured_git_model_baseline_dir() or Path(experiment_dir).resolve()


def _active_model_contract() -> dict[str, Any]:
    cfg = get_config().mcp.git
    if cfg.enabled and cfg.scope == "baseline_model":
        try:
            repo_cfg = cfg.resolve_repo()
            return {
                "repo_id": repo_cfg.repo_id,
                "model_root": repo_cfg.model.root,
                "copy_include": list(repo_cfg.model.copy_include),
                "copy_exclude": list(repo_cfg.model.copy_exclude),
                "publish_paths": list(repo_cfg.model.publish_paths or repo_cfg.allowed_paths),
                "requirements_paths": list(repo_cfg.model.requirements_paths),
                "entrypoint_candidates": list(repo_cfg.model.entrypoint_candidates),
                "default_train_command": list(repo_cfg.model.default_train_command),
                "output_contract": dict(repo_cfg.model.output_contract),
            }
        except Exception as exc:
            return {"error": f"model contract unavailable: {exc}"}
    return {
        "repo_id": "local_baseline",
        "model_root": str(get_paths().cfg.roots.baseline),
        "copy_include": ["src/**", "requirements.txt", "train.py"],
        "copy_exclude": ["data/**", "outputs/**", "logs/**", "__pycache__/**", "*.pyc"],
        "publish_paths": list(get_paths().cfg.model.publish_allowed_paths),
        "requirements_paths": [get_paths().cfg.model.requirements],
        "entrypoint_candidates": ["train.py", "src/train.py", "main.py"],
        "default_train_command": [],
        "output_contract": _default_output_contract(),
    }


def _default_output_contract() -> dict[str, Any]:
    return {
        "prediction_path": "{trial_id}_package_detail.csv",
        "actual_path": "",
        "split_column": "split",
        "split_filter": "test",
        "actual_column": "",
        "prediction_column": "",
        "actual_candidates": ["true_pos_cnt", "true_real_qty_sum", "actual", "y_true"],
        "prediction_candidates": ["pred_pos_cnt", "pred_real_qty_sum", "prediction", "y_pred"],
        "error_column": "",
        "abs_error_column": "",
        "baseline_prediction_globs": ["*_package_detail.csv", "*.csv"],
        "secondary_metric_globs": ["*_store_dish_day.csv"],
        "primary_level": "package_detail",
        "secondary_level": "store_dish_day",
    }


def _model_contract_prompt() -> str:
    return json.dumps(_active_model_contract(), indent=2, ensure_ascii=False)


def copy_source_to_trial(experiment_dir: str, output_dir: str) -> list[str]:
    """将实验目录可执行源码复制到 trial 目录, 返回复制的文件列表"""
    src = Path(experiment_dir).resolve()
    dst = _trial_code_dir(output_dir)
    dst.mkdir(parents=True, exist_ok=True)

    git_paths = _configured_git_model_paths()
    if git_paths and src == git_paths["baseline"]:
        copied: list[str] = []
        include = list(git_paths.get("copy_include") or ["**"])
        exclude = list(git_paths.get("copy_exclude") or [])
        for f in src.rglob("*"):
            if not f.is_file():
                continue
            if "__pycache__" in f.parts or f.suffix == ".pyc":
                continue
            rel = f.relative_to(src).as_posix()
            if not _matches_copy_spec(rel, include):
                continue
            if _matches_copy_spec(rel, exclude):
                continue
            target = dst / rel
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            copied.append(rel)
        return sorted(copied)

    # 永远跳过的目录
    SKIP_PARTS = (
        ".venv", "node_modules", "archive", ".comboscope_backups",
        "__pycache__", ".git", ".claude", "runs", "data", "outputs", "logs",
    )
    ALLOWED_TOP = {"src", "requirements.txt", "train.py"}

    copied: list[str] = []
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        if any(skip in f.parts for skip in SKIP_PARTS):
            continue
        rel = f.relative_to(src)
        if rel.parts and rel.parts[0] not in ALLOWED_TOP:
            continue
        target = dst / rel
        if target.exists():
            continue
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
    Normalize legacy/misplaced trial code from <trial>/code to the configured candidate code dir.

    Older skill instructions used runs/<trial>/code. The canonical layout is now
    configured by flow_paths.yaml. This guard makes the workflow tolerant to one bad
    agent write while still keeping T3 strict about the canonical location.
    """
    out = Path(output_dir)
    legacy = out / "code"
    canonical = _trial_code_dir(output_dir)
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
        print(f"[{phase}] normalized misplaced code/ -> {get_paths().cfg.trial.code_dir}/: {moved}")
    return moved


def _data_file_ref(path: Path) -> dict[str, Any]:
    exists = path.exists()
    ref: dict[str, Any] = {
        "path": get_paths().rel(path),
        "absolute_path": str(path),
        "exists": exists,
        "copied": False,
    }
    if not exists:
        return ref
    stat = path.stat()
    ref.update({
        "size_bytes": stat.st_size,
        "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime)),
    })
    try:
        with path.open("rb") as f:
            header = f.readline().strip()
        ref["header_sha256"] = hashlib.sha256(header).hexdigest()
        try:
            ref["columns"] = header.decode("utf-8-sig").split(",")
        except UnicodeDecodeError:
            ref["columns"] = []
    except Exception as e:
        ref["header_error"] = str(e)
    return ref


def _write_data_refs(output_dir: str) -> dict[str, Any]:
    paths = get_paths()
    inputs_dir = _trial_inputs_dir(output_dir)
    inputs_dir.mkdir(parents=True, exist_ok=True)
    refs = {
        "mode": paths.cfg.data.mode,
        "copied": False,
        "primary": _data_file_ref(paths.data_primary()),
        "auxiliary": [_data_file_ref(item) for item in paths.data_auxiliary()],
    }
    (inputs_dir / "data_refs.json").write_text(
        json.dumps(refs, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return refs


def _copy_data_dir(data_dir: str, output_dir: str) -> None:
    """Compatibility no-op: training data is referenced, not copied to trials."""
    _write_data_refs(output_dir)


def _assert_candidate_code_clean(output_dir: str) -> None:
    code_dir = _trial_code_dir(output_dir)
    forbidden = []
    for name in ("data", "outputs", "logs", "__pycache__"):
        path = code_dir / name
        if path.exists():
            forbidden.append(_rel_to_trial(output_dir, path))
    if forbidden:
        raise RuntimeError(f"candidate code contains forbidden directories: {forbidden}")


# ============================================================
# 子线程执行
# ============================================================

def _ensure_dirs(output_dir: str) -> None:
    """确保产物子目录存在 (与 skills 期望对齐)"""
    paths = get_paths().cfg.trial
    for sub in [paths.inputs_dir, paths.outputs_dir, paths.code_dir, paths.legacy_code_dir,
                "agent1", "agent2", paths.evaluation_dir, paths.reports_dir,
                paths.standardized_dir, paths.logs_dir, "audit"]:
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


def _is_request_timeout(error: BaseException | str) -> bool:
    msg = str(error).lower()
    return (
        isinstance(error, TimeoutError)
        or "request timed out" in msg
        or "timed out" in msg
        or "timeout" in msg
    )


def _start_and_run_codex_thread(
    codex: Codex,
    spec: ThreadSpec,
    inputs: list,
    output_dir: str,
    *,
    max_attempts: int,
    retry_delay_s: float,
) -> tuple[Any, Any]:
    attempts = max(1, max_attempts)
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        thread = codex.thread_start(
            cwd=output_dir,
            sandbox=Sandbox.full_access,
            approval_mode=ApprovalMode.deny_all,
        )
        print(f"[{spec.phase}] 绛夊緟 LLM 鍝嶅簲 (attempt {attempt}/{attempts}, 鍙兘闇€瑕?30s-2min)...")
        try:
            result = thread.run(
                inputs,
                sandbox=Sandbox.full_access,
                approval_mode=ApprovalMode.deny_all,
            )
            return thread, result
        except Exception as e:
            last_error = e
            _dump_codex_stderr(codex, f"{spec.phase}_exception")
            _stage_log(output_dir, spec.phase, f"attempt {attempt} exception: {e}")
            if _is_request_timeout(e) and attempt < attempts:
                _stage_log(output_dir, spec.phase, f"retry after timeout in {retry_delay_s:.0f}s")
                time.sleep(retry_delay_s)
                continue
            raise
    raise RuntimeError(f"[{spec.phase}] no Codex result: {last_error}")


def _run_codex_turn_with_retry(
    codex: Codex,
    thread: Any,
    spec: ThreadSpec,
    inputs: list,
    output_dir: str,
    *,
    max_attempts: int,
    retry_delay_s: float,
) -> Any:
    attempts = max(1, max_attempts)
    current_thread = thread
    for attempt in range(1, attempts + 1):
        try:
            return current_thread.run(
                inputs,
                sandbox=Sandbox.full_access,
                approval_mode=ApprovalMode.deny_all,
            )
        except Exception as e:
            _dump_codex_stderr(codex, f"{spec.phase}_exception")
            _stage_log(output_dir, spec.phase, f"attempt {attempt} exception: {e}")
            if _is_request_timeout(e) and attempt < attempts:
                _stage_log(output_dir, spec.phase, f"retry after timeout in {retry_delay_s:.0f}s")
                time.sleep(retry_delay_s)
                current_thread = codex.thread_resume(thread.id)
                continue
            raise
    raise RuntimeError(f"[{spec.phase}] no Codex result")


def run_codex_thread(
    codex: Codex,
    spec: ThreadSpec,
    *,
    experiment_dir: str,
    output_dir: str,
    ask: str,
    trial_id: str,
    data_dir: str = "",
    data_path: str = "",
    trial_code_dir: str = "",
    legacy_trial_code_dir: str = "",
    trial_outputs_dir: str = "",
    model_contract: str = "",
    max_attempts: int = 2,
    retry_delay_s: float = 15.0,
) -> str:
    """启动并执行一个 Codex 子线程, 返回 thread_id"""
    prompt = spec.prompt.format(
        experiment_dir=experiment_dir,
        output_dir=output_dir,
        ask=ask,
        trial_id=trial_id,
        data_dir=data_dir,
        data_path=data_path,
        trial_code_dir=trial_code_dir,
        legacy_trial_code_dir=legacy_trial_code_dir,
        trial_outputs_dir=trial_outputs_dir,
        model_contract=model_contract,
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
    result = _run_codex_turn_with_retry(
        codex,
        thread,
        spec,
        inputs,
        output_dir,
        max_attempts=max_attempts,
        retry_delay_s=retry_delay_s,
    )

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

def _read_csv_header(csv_path: Path) -> list[str]:
    import csv

    with csv_path.open("r", newline="", encoding="utf-8-sig", errors="replace") as handle:
        try:
            return next(csv.reader(handle))
        except StopIteration:
            return []


def _output_contract(exec_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    contract = _default_output_contract()
    active = _active_model_contract().get("output_contract", {})
    if isinstance(active, dict):
        contract.update({k: v for k, v in active.items() if v is not None})
    plan_contract = (exec_plan or {}).get("output_contract", {})
    if isinstance(plan_contract, dict):
        contract.update({k: v for k, v in plan_contract.items() if v is not None})
    return contract


def _format_contract_value(value: Any, output: Path, trial_id: str) -> str:
    return str(value or "").format(
        output_dir=str(output),
        trial_id=trial_id,
        data_path=str(get_paths().data_primary()),
        trial_code_dir=str(get_paths().trial_code_dir(output)),
        trial_outputs_dir=str(get_paths().trial_outputs_dir(output)),
        trial_standardized_dir=str(get_paths().trial_standardized_dir(output)),
    )


def _resolve_contract_path(value: Any, output: Path, trial_id: str, *, default_base: Path) -> Path:
    formatted = _format_contract_value(value, output, trial_id)
    path = Path(formatted)
    if path.is_absolute():
        return path.resolve()
    normalized = formatted.replace("\\", "/")
    for prefix in ("standardized/", "evaluation/", "outputs/", "agent2/", "candidate/"):
        if normalized.startswith(prefix):
            return (output / path).resolve()
    return (default_base / path).resolve()


def _validate_t3_prediction_contract(csv_path: Path, contract: dict[str, Any] | None = None) -> None:
    """Reject obviously invalid codegen outputs before expensive metric reads."""
    contract = contract or _default_output_contract()
    if not csv_path.exists():
        raise PredictionContractError(f"prediction file does not exist: {csv_path}")

    size = csv_path.stat().st_size
    if size > MAX_T3_PREDICTION_BYTES:
        raise PredictionContractError(
            f"prediction file is too large for T3 evaluation: {size} bytes "
            f"(limit={MAX_T3_PREDICTION_BYTES}); likely wrote the raw feature table"
        )

    columns = set(_read_csv_header(csv_path))
    if not columns:
        raise PredictionContractError(f"prediction file has no header: {csv_path}")

    split_column = str(contract.get("split_column") or "split")
    if split_column and split_column not in columns:
        raise PredictionContractError(
            f"prediction output missing required split column {split_column!r}; columns={sorted(columns)}"
        )

    configured_actual = str(contract.get("actual_column") or "")
    configured_prediction = str(contract.get("prediction_column") or "")
    if configured_actual or configured_prediction:
        missing = [col for col in (configured_actual, configured_prediction) if col and col not in columns]
        if missing:
            raise PredictionContractError(
                f"prediction output missing configured metric columns {missing}; columns={sorted(columns)}"
            )
        if configured_actual and configured_prediction:
            return

    actual_candidates = [str(item) for item in contract.get("actual_candidates", []) if item]
    prediction_candidates = [str(item) for item in contract.get("prediction_candidates", []) if item]
    for actual_col in actual_candidates:
        for pred_col in prediction_candidates:
            if actual_col in columns and pred_col in columns:
                return

    error_col = str(contract.get("error_column") or "")
    abs_error_col = str(contract.get("abs_error_column") or "")
    if error_col and abs_error_col and error_col in columns and abs_error_col in columns:
        if configured_actual and configured_actual in columns:
            return
        if any(actual_col in columns for actual_col in actual_candidates):
            return

    legacy_schemas = (
        {"true_pos_cnt", "error_pos_cnt", "abs_error_pos_cnt"},
        {"true_real_qty_sum", "error", "abs_error"},
    )
    if any(schema.issubset(columns) for schema in legacy_schemas):
        return

    raise PredictionContractError(
        "prediction output does not match output_contract metric columns; "
        f"columns={sorted(columns)}, contract={contract}"
    )


def _train_cmd_uses_history_eval_only(train_cmd: list[str]) -> bool:
    return any(str(part) in {"--history_eval_only", "--history-eval-only"} for part in train_cmd)


def _extract_top_level_function_source(source: str, function_name: str) -> list[str]:
    lines = source.splitlines()
    start_idx: int | None = None
    for idx, line in enumerate(lines):
        if line.startswith(f"def {function_name}("):
            start_idx = idx
            break
    if start_idx is None:
        return lines

    end_idx = len(lines)
    for idx in range(start_idx + 1, len(lines)):
        line = lines[idx]
        if line.startswith("def ") or line.startswith("class "):
            end_idx = idx
            break
    return lines[start_idx:end_idx]


def _source_files_for_history_eval_check(code_dir: Path, exec_plan: dict[str, Any]) -> list[Path]:
    candidates: list[Path] = []
    source_entrypoint = exec_plan.get("source_entrypoint")
    if source_entrypoint:
        candidates.append(code_dir / str(source_entrypoint))
    candidates.append(code_dir / "train.py")

    seen: set[Path] = set()
    existing: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.exists():
            existing.append(candidate)
    return existing


def _validate_history_eval_bounded_path(code_dir: Path, exec_plan: dict[str, Any], train_cmd: list[str]) -> None:
    """Reject candidate code that would run full dish allocation in --history_eval_only.

    This is intentionally a conservative preflight check. It only activates for
    train commands that include --history_eval_only, and it scans the generated
    model entrypoint for the expected bounded early-return before expensive
    allocation / store-dish output calls.
    """
    if not _train_cmd_uses_history_eval_only(train_cmd):
        return

    source_files = _source_files_for_history_eval_check(code_dir, exec_plan)
    if not source_files:
        raise HistoryEvalContractError(
            "cannot find generated train.py or source_entrypoint to verify bounded history_eval_only path"
        )

    dangerous_tokens = (
        "allocate_dish_prediction_by_target_date(",
        "build_store_dish_day_output(",
        "store_dish_day_df.to_csv(",
        "_store_dish_day.csv",
    )
    guard_tokens = (
        "if args.history_eval_only",
        "if getattr(args, \"history_eval_only\"",
        "if getattr(args, 'history_eval_only'",
    )

    checked_any_danger = False
    for source_path in source_files:
        source = source_path.read_text(encoding="utf-8", errors="replace")
        lines = _extract_top_level_function_source(source, "run_t2_package_backtest")

        dangerous_lines: list[int] = []
        guard_lines: list[int] = []
        for idx, raw_line in enumerate(lines):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("def "):
                continue
            if any(token in stripped for token in guard_tokens):
                guard_lines.append(idx)
            if any(token in stripped for token in dangerous_tokens):
                dangerous_lines.append(idx)

        if not dangerous_lines:
            continue
        checked_any_danger = True
        first_danger = min(dangerous_lines)
        valid_guard = False
        for guard in guard_lines:
            if guard >= first_danger:
                continue
            guarded_block = lines[guard:first_danger]
            if any("return" in line.strip().split("#", 1)[0] for line in guarded_block):
                valid_guard = True
                break
        if not valid_guard:
            source_rel = source_path.relative_to(code_dir) if source_path.is_relative_to(code_dir) else source_path
            raise HistoryEvalContractError(
                f"{source_rel} calls expensive dish allocation/store_dish output before a bounded "
                "--history_eval_only early return; add an if args.history_eval_only block that writes "
                "test package_detail contract columns and returns before allocation"
            )

    if checked_any_danger:
        return


def _append_train_log_section(train_log: Path, title: str, body: str) -> None:
    existing = train_log.read_text(encoding="utf-8") if train_log.exists() else ""
    train_log.write_text(
        f"{existing.rstrip()}\n\n=== {title} ===\n{body.rstrip()}\n",
        encoding="utf-8",
    )


def _timeout_output_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _classify_eval_failure(eval_error: str | None) -> str | None:
    if not eval_error:
        return None
    if eval_error.startswith("history_eval_only contract invalid"):
        return "history_eval_contract_error"
    if eval_error.startswith("training command timed out"):
        return "train_timeout"
    if eval_error.startswith("training failed"):
        return "train_error"
    if eval_error.startswith("prediction contract invalid"):
        return "output_contract_error"
    if "timed out" in eval_error:
        return "metric_timeout"
    if eval_error.startswith("prediction file missing"):
        return "missing_prediction"
    if eval_error.startswith("calculate_metrics.py failed"):
        return "metric_error"
    return "eval_error"


def _needs_codegen_retry(comparison: dict | None) -> bool:
    if not comparison:
        return True
    return comparison.get("primary", {}).get("new_wape", 999) == 999


def _compute_wape_bias_from_csv(
    csv_path: Path,
    split_filter: str = "test",
    contract: dict[str, Any] | None = None,
) -> tuple[float, float, int]:
    """从带 split 列的 prediction CSV 计算指定 split 的 WAPE 和 Bias."""
    import pandas as pd
    contract = contract or _default_output_contract()
    df = pd.read_csv(csv_path)

    split_column = str(contract.get("split_column") or "split")
    split_value = str(contract.get("split_filter") or split_filter)
    if split_column and split_column in df.columns:
        df = df[df[split_column] == split_value]
    if len(df) == 0:
        raise ValueError(f"{split_column}='{split_value}' 无数据")

    configured_actual = str(contract.get("actual_column") or "")
    configured_prediction = str(contract.get("prediction_column") or "")
    if configured_actual and configured_prediction and configured_actual in df.columns and configured_prediction in df.columns:
        actual_col, pred_col = configured_actual, configured_prediction
    else:
        actual_col = pred_col = ""
        actual_candidates = [str(item) for item in contract.get("actual_candidates", []) if item]
        prediction_candidates = [str(item) for item in contract.get("prediction_candidates", []) if item]
        for candidate_actual in actual_candidates:
            for candidate_prediction in prediction_candidates:
                if candidate_actual in df.columns and candidate_prediction in df.columns:
                    actual_col, pred_col = candidate_actual, candidate_prediction
                    break
            if actual_col and pred_col:
                break

    if actual_col and pred_col:
        errors = df[pred_col] - df[actual_col]
        actual_sum = df[actual_col].sum()
        wape = errors.abs().sum() / actual_sum if actual_sum != 0 else float("nan")
        bias = errors.sum() / actual_sum if actual_sum != 0 else float("nan")
        return float(wape), float(bias), len(df)

    error_col = str(contract.get("error_column") or "")
    abs_error_col = str(contract.get("abs_error_column") or "")
    actual_for_error = configured_actual or next(
        (str(item) for item in contract.get("actual_candidates", []) if str(item) in df.columns),
        "",
    )
    if error_col and abs_error_col and actual_for_error and error_col in df.columns and abs_error_col in df.columns:
        denom = df[actual_for_error].sum()
        wape = df[abs_error_col].sum() / denom if denom != 0 else float("nan")
        bias = df[error_col].sum() / denom if denom != 0 else float("nan")
        return float(wape), float(bias), len(df)

    if "abs_error_pos_cnt" in df.columns and "true_pos_cnt" in df.columns:
        wape = df["abs_error_pos_cnt"].sum() / df["true_pos_cnt"].sum()
        bias = df["error_pos_cnt"].sum() / df["true_pos_cnt"].sum()
        return float(wape), float(bias), len(df)
    if "abs_error" in df.columns and "true_real_qty_sum" in df.columns:
        wape = df["abs_error"].sum() / df["true_real_qty_sum"].sum()
        bias = df["error"].sum() / df["true_real_qty_sum"].sum()
        return float(wape), float(bias), len(df)

    raise ValueError(f"无法识别列, 可用: {list(df.columns)}, contract={contract}")


def _resolve_train_command(train_cmd: list[Any], output: Path, trial_id: str) -> list[str]:
    """Resolve execution-plan train command against the canonical candidate/code layout."""
    paths = get_paths()
    resolved = [
        str(arg).format(
            output_dir=str(output),
            trial_id=trial_id,
            data_path=str(paths.data_primary()),
            trial_code_dir=str(paths.trial_code_dir(output)),
            trial_outputs_dir=str(paths.trial_outputs_dir(output)),
        )
        for arg in train_cmd
    ]
    if not resolved:
        return resolved

    code_dir = _existing_code_dir(output)
    executable = Path(resolved[0]).name.lower()
    if executable in {"python", "python.exe", "python3", "python3.exe"}:
        resolved[0] = sys.executable
        if len(resolved) > 1 and resolved[1] not in {"-m", "-c"}:
            script_text = resolved[1]
            script = Path(script_text)
            if not script.is_absolute():
                normalized = script_text.replace("\\", "/")
                candidates: list[Path] = []
                if normalized.startswith(get_paths().cfg.trial.code_dir + "/"):
                    candidates.append(output / script)
                if normalized.startswith(get_paths().cfg.trial.legacy_code_dir + "/"):
                    candidates.append(output / script)
                candidates.extend([
                    code_dir / script,
                    _trial_code_dir(output) / script,
                    _legacy_code_dir(output) / script,
                    output / script,
                ])
                for candidate in candidates:
                    if candidate.exists():
                        script = candidate
                        break
                else:
                    legacy_prefix = get_paths().cfg.trial.legacy_code_dir + "/"
                    code_prefix = get_paths().cfg.trial.code_dir + "/"
                    script = candidates[0] if normalized.startswith((legacy_prefix, code_prefix)) else code_dir / script
            resolved[1] = str(script.resolve(strict=False))

    def _set_or_append(flag: str, value: str) -> None:
        if flag in resolved:
            idx = resolved.index(flag)
            if idx + 1 < len(resolved):
                resolved[idx + 1] = value
                return
        resolved.extend([flag, value])

    _set_or_append("--data_path", str(paths.data_primary()))
    _set_or_append("--output_dir", str(paths.trial_outputs_dir(output)))

    return resolved


def _read_json_file(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _write_fallback_report(output_dir: str, error: str) -> None:
    output = Path(output_dir)
    comparison = _read_json_file(output / "evaluation" / "metric_comparison.json", {})
    run_status = _read_json_file(output / "agent2" / "run_status.json", {})
    primary = comparison.get("primary", {})
    secondary = comparison.get("secondary", {})
    decision = str(comparison.get("decision", "rollback")).upper()
    reason = comparison.get("reason", f"T4 report fallback after error: {error}")
    lines = [
        "# 实验验证结论报告",
        "",
        "> 本报告由 Python fallback 生成，因为 Codex T4 report 阶段未完成。",
        "",
        "## 1. 决策",
        "",
        f"- 决策: **{decision}**",
        f"- 原因: {reason}",
        f"- T4 异常: `{error}`",
        "",
        "## 2. 核心指标",
        "",
        "| 指标 | Baseline | New | Delta |",
        "|------|----------|-----|-------|",
        (
            f"| WAPE | {primary.get('old_wape', 999):.4f} | "
            f"{primary.get('new_wape', 999):.4f} | "
            f"{comparison.get('wape_delta', 0):+.4f} |"
        ),
        (
            f"| Bias | {primary.get('old_bias', 0):+.4f} | "
            f"{primary.get('new_bias', 0):+.4f} | "
            f"{comparison.get('bias_delta', 0):+.4f} |"
        ),
        "",
        "## 3. 辅助指标",
        "",
        f"- {secondary.get('level', 'secondary')} baseline WAPE: {secondary.get('old_wape', 999):.4f}",
        f"- {secondary.get('level', 'secondary')} baseline Bias: {secondary.get('old_bias', 0):+.4f}",
        "",
        "## 4. 执行状态",
        "",
        f"- 训练成功: {run_status.get('train_success', '?')}",
        f"- 评估成功: {run_status.get('eval_success', '?')}",
        f"- 预测文件: `{run_status.get('prediction_path', '')}`",
        f"- 训练日志: `{run_status.get('train_log_path', 'logs/train.log')}`",
        "",
        "## 5. 产物索引",
        "",
        "- `agent2/experiment_review.md`",
        "- `agent2/review_result.json`",
        "- `evaluation/metric_comparison.json`",
        "- `agent2/agent2_execution_plan.yaml`",
        "- `workflow_manifest.json`",
    ]
    (output / "final_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _stage_log(output_dir, "report", f"fallback final_report.md generated after: {error}")


def _write_fallback_feishu_card(output_dir: str, error: str) -> None:
    output = Path(output_dir)
    comparison = _read_json_file(output / "evaluation" / "metric_comparison.json", {})
    primary = comparison.get("primary", {})
    decision = str(comparison.get("decision", "rollback")).upper()
    lines = [
        f"**实验完成: {output.name}**",
        "",
        f"**决策: {decision}**",
        "",
        "| 指标 | Baseline | New | Delta |",
        "|------|----------|-----|-------|",
        (
            f"| WAPE | {primary.get('old_wape', 999):.4f} | "
            f"{primary.get('new_wape', 999):.4f} | "
            f"{comparison.get('wape_delta', 0):+.4f} |"
        ),
        (
            f"| Bias | {primary.get('old_bias', 0):+.4f} | "
            f"{primary.get('new_bias', 0):+.4f} | "
            f"{comparison.get('bias_delta', 0):+.4f} |"
        ),
        "",
        f"T5 fallback: `{error}`",
        "",
        "请审核后回复 `/keep`、`/rollback`、`/reverse` 或 `/stop`。",
    ]
    (output / "feishu_review_card.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _stage_log(output_dir, "feishu-card", f"fallback feishu_review_card.md generated after: {error}")


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
    code_dir = _existing_code_dir(output)
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
    train_log = get_paths().trial_logs_dir(output) / "train.log"
    train_error: str | None = None
    try:
        _validate_history_eval_bounded_path(code_dir, exec_plan, train_cmd)
    except HistoryEvalContractError as e:
        train_error = f"history_eval_only contract invalid: {e}"
        print(f"[T3] history_eval_only contract invalid: {e}")
        _stage_log(str(output), "execute", train_error)
        train_result = subprocess.CompletedProcess(
            args=train_cmd,
            returncode=125,
            stdout="",
            stderr=train_error,
        )
    else:
        try:
            train_result = subprocess.run(
                train_cmd,
                cwd=str(code_dir),
                capture_output=True, text=True,
                timeout=T3_TRAIN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as e:
            stdout = _timeout_output_to_text(e.stdout)
            stderr = _timeout_output_to_text(e.stderr)
            train_error = f"training command timed out after {e.timeout} seconds"
            print(f"[T3] training timeout: {train_error}")
            _stage_log(str(output), "execute", train_error)
            train_result = subprocess.CompletedProcess(
                args=train_cmd,
                returncode=124,
                stdout=stdout,
                stderr=(stderr.rstrip() + f"\n{train_error}").strip(),
            )

    _append_train_log_section(
        train_log,
        "T3 TRAIN RUN",
        (
            f"CMD: {' '.join(train_cmd)}\n"
            f"RC: {train_result.returncode}\n\n"
            f"STDOUT:\n{train_result.stdout}\n\n"
            f"STDERR:\n{train_result.stderr}"
        ),
    )

    train_success = train_result.returncode == 0
    print(f"[T3] 训练 {'成功' if train_success else '失败'} (rc={train_result.returncode})")
    _stage_log(str(output), "execute", f"training finished rc={train_result.returncode}")

    # ── 3. 运行评测 ──
    output_contract = _output_contract(exec_plan)
    prediction_path = _resolve_contract_path(
        output_contract.get("prediction_path", "new_prediction.csv"),
        output,
        manifest.trial_id,
        default_base=_trial_outputs_dir(output),
    )
    actual_contract_path = output_contract.get("actual_path")
    actual_path = (
        _resolve_contract_path(actual_contract_path, output, manifest.trial_id, default_base=_trial_outputs_dir(output))
        if actual_contract_path
        else get_paths().trial_standardized_dir(output) / "standardized_actual.csv"
    )
    split_filter = str(output_contract.get("split_filter") or "test")

    eval_success = False
    eval_error: str | None = None
    new_wape, new_bias, new_rows = 999.0, 0.0, 0  # 默认值: 训练失败时使用
    if train_success and prediction_path.exists():
        try:
            _validate_t3_prediction_contract(prediction_path, output_contract)
            new_wape, new_bias, new_rows = _compute_wape_bias_from_csv(prediction_path, split_filter, output_contract)
            eval_success = True
            print(f"[T3] new test: WAPE={new_wape:.4f}, Bias={new_bias:+.4f}, rows={new_rows}")
            _stage_log(str(output), "execute", f"new test WAPE={new_wape:.4f} Bias={new_bias:+.4f} rows={new_rows}")
            (output / "evaluation" / "new_metrics.json").write_text(
                json.dumps({"wape": new_wape, "bias": new_bias, "rows": new_rows}, indent=2, ensure_ascii=False),
                encoding="utf-8")
        except PredictionContractError as e:
            eval_error = f"prediction contract invalid: {e}"
            print(f"[T3] prediction contract invalid: {e}")
            _stage_log(str(output), "execute", eval_error)
        except Exception as e:
            print(f"[T3] test-split 读取失败 ({e}), 回退 calculate_metrics.py")
            new_metrics_csv = output / "evaluation" / "new_metrics_summary.csv"
            try:
                eval_result = subprocess.run(
                    [sys.executable, str(EVAL_CALCULATE_SCRIPT),
                     "--prediction", str(prediction_path),
                     "--actual", str(actual_path),
                     "--output", str(new_metrics_csv)],
                    capture_output=True, text=True, timeout=T3_METRIC_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as e:
                eval_error = f"calculate_metrics.py timed out after {e.timeout} seconds"
                print(f"[T3] evaluation timeout: {eval_error}")
                _stage_log(str(output), "execute", eval_error)
                eval_result = subprocess.CompletedProcess(args=[], returncode=124, stdout="", stderr=eval_error)
            eval_success = bool(eval_result and eval_result.returncode == 0)
            if not eval_success and eval_result is not None and eval_error is None:
                eval_error = f"calculate_metrics.py failed rc={eval_result.returncode}: {eval_result.stderr[:500]}"
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

    if not train_success:
        if train_error:
            eval_error = train_error
        else:
            stderr_tail = (train_result.stderr or train_result.stdout or "").strip()[-500:]
            eval_error = f"training failed rc={train_result.returncode}: {stderr_tail}"

    if train_success and not prediction_path.exists():
        eval_error = f"prediction file missing after successful training: {prediction_path}"

    failure_type = _classify_eval_failure(eval_error)
    if eval_error:
        if failure_type == "history_eval_contract_error":
            failure_section = "T3 PREFLIGHT CONTRACT ERROR"
        elif failure_type in {"train_timeout", "train_error"}:
            failure_section = "T3 TRAINING ERROR"
        else:
            failure_section = "T3 EVALUATION ERROR"
        _append_train_log_section(
            train_log,
            failure_section,
            (
                f"{eval_error}\n"
                f"Prediction path: {prediction_path}\n"
                f"Output contract: {json.dumps(output_contract, ensure_ascii=False)}\n"
                "Codegen must repair the candidate code before retry.\n"
                "Write an output file that satisfies output_contract: split filtering plus "
                "configured actual/prediction columns or accepted candidate columns.\n"
                "Do not write the raw feature table as the evaluation output.\n"
                "If this is a train_timeout, do not extend the timeout; implement a bounded "
                "evaluation path that finishes quickly on the full data reference."
            ),
        )
        (output / "evaluation" / "new_metrics.json").write_text(
            json.dumps(
                {
                    "wape": new_wape,
                    "bias": new_bias,
                    "rows": new_rows,
                    "error": eval_error,
                    "failure_type": failure_type,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    # ── 4. 读取 baseline 旧指标 (按 output_contract 从 outputs/ 选择候选) ──
    primary_level = str(output_contract.get("primary_level") or "prediction")
    secondary_level = str(output_contract.get("secondary_level") or "secondary")
    old_wape, old_bias, old_rows = 999.0, 0.0, 0
    old_wape_dish, old_bias_dish = 999.0, 0.0

    # 旧指标来源: 链式执行用 baseline_metric_dir, 否则用 experiment_dir/outputs/
    metric_dir = Path(baseline_metric_dir) if baseline_metric_dir else Path(manifest.experiment_dir) / "outputs"
    print(f"[T3] 读取 baseline 指标: {metric_dir}")

    # 主指标: output_contract 指定的 baseline 候选
    pkg_files: list[Path] = []
    for pattern in output_contract.get("baseline_prediction_globs", []) or ["*.csv"]:
        pkg_files.extend(sorted(metric_dir.glob(str(pattern))))
    # 去重且保持顺序
    pkg_files = list(dict.fromkeys(pkg_files))
    if pkg_files:
        try:
            old_wape, old_bias, old_rows = _compute_wape_bias_from_csv(pkg_files[0], split_filter, output_contract)
            print(f"[T3] baseline {primary_level} {split_filter}: WAPE={old_wape:.4f}, Bias={old_bias:+.4f}, rows={old_rows}")
        except Exception as e:
            print(f"[T3] 警告: 读取 {primary_level} 失败 ({e})")

    # 辅助指标: output_contract 可选 secondary_metric_globs
    dish_files: list[Path] = []
    for pattern in output_contract.get("secondary_metric_globs", []) or []:
        dish_files.extend(sorted(metric_dir.glob(str(pattern))))
    dish_files = list(dict.fromkeys(dish_files))
    if dish_files:
        try:
            old_wape_dish, old_bias_dish, _ = _compute_wape_bias_from_csv(dish_files[0], split_filter, output_contract)
            print(f"[T3] baseline {secondary_level} {split_filter}: WAPE={old_wape_dish:.4f}, Bias={old_bias_dish:+.4f}")
        except Exception as e:
            print(f"[T3] 警告: 读取 {secondary_level} 失败 ({e})")

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
        "reason": (f"{primary_level} WAPE delta={wape_delta:.4f}, Bias delta={bias_delta:.4f}, "
                   f"train_ok={train_success}, eval_ok={eval_success}"),
        "wape_delta": wape_delta,
        "bias_delta": bias_delta,
        "eval_error": eval_error,
        "failure_type": failure_type,
        "primary": {
            "level": primary_level,
            "old_wape": old_wape, "new_wape": new_wape,
            "old_bias": old_bias, "new_bias": new_bias,
            "old_rows": old_rows,
        },
        "secondary": {
            "level": secondary_level,
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
        "eval_error": eval_error,
        "failure_type": failure_type,
        "prediction_path": str(prediction_path),
        "actual_path": str(actual_path),
        "output_contract": output_contract,
        "train_log_path": str(get_paths().trial_logs_dir(output) / "train.log"),
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
见 candidate/code/train.py
"""
    (output / "agent2" / "experiment_review.md").write_text(review_md, encoding="utf-8")

    print(
        f"\n[T3] 决策: {decision.upper()} | "
        f"{primary_level} WAPE {old_wape:.4f}→{new_wape:.4f} ({wape_delta:+.4f}) | "
        f"Bias {old_bias:+.4f}→{new_bias:+.4f} ({bias_delta:+.4f})"
    )
    if old_wape_dish != 999:
        print(f"[T3]     {secondary_level} WAPE={old_wape_dish:.4f} (辅助参考)")

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
    resume: bool = True,
    resume_from_phase: str | None = None,
    data_path: str | None = None,
) -> WorkflowManifest:
    """
    ComboScope Codex 多线程工作流 — 单次 Codex Session

    流程:
      认证 → T1 (evaluate) → T2a (plan) → [copy source] → T2b (codegen)
      → T3 (execute, Python) → T4 (report)

    链式执行: --experiment baseline --previous-trial runs/trial_N
      - 代码从 previous_trial candidate/code/ 继承 (保留 codegen 修改)
      - 数据路径来自 flow_paths.yaml 或运行时 data_path 覆盖
      - baseline 指标从 previous_trial/outputs/ 读取

    codex 参数: 传入已认证的 Codex 实例时, 跳过 session 创建和认证步骤,
    直接复用已有 session。用于循环模式 (run_loop) 避免重复启动 app-server。
    """
    if data_path:
        override_data_primary(data_path)

    trial_id = Path(output_dir).name
    exp_dir = str(Path(experiment_dir).resolve())
    out_dir = str(Path(output_dir).resolve())
    prev_dir = str(Path(previous_trial).resolve()) if previous_trial else None
    is_chain = prev_dir is not None

    _ensure_dirs(output_dir)
    _normalize_trial_code_layout(output_dir, phase="layout-preflight")
    existing_manifest = WorkflowManifest.load(output_dir) if resume else None
    if existing_manifest:
        manifest = existing_manifest
        manifest.experiment_dir = exp_dir
        manifest.output_dir = out_dir
        manifest.ask = ask
    else:
        manifest = WorkflowManifest(
            trial_id=trial_id,
            experiment_dir=exp_dir,
            output_dir=out_dir,
            ask=ask,
        )
    manifest._write()
    if prev_dir:
        manifest.threads["_chain"] = {"previous_trial": prev_dir}
        manifest._write()
    manifest.paths = get_paths().manifest_summary(out_dir)
    manifest._write()

    # 代码来源: 链式执行从上一轮 candidate/code/ 继承, 否则从 experiment_dir 复制
    code_src_dir = str(_existing_code_dir(prev_dir)) if is_chain else str(_initial_code_source_dir(exp_dir))
    # 数据目录来自 flow_paths.yaml 或运行时 data_path 覆盖
    data_dir = str(get_paths().abs(get_paths().cfg.data.root))
    # 旧指标来源: 链式执行读上一轮的 outputs, 否则读 baseline/outputs
    baseline_metric_dir = str(_trial_outputs_dir(prev_dir)) if is_chain else str(Path(exp_dir) / "outputs")

    # 线程公共参数
    recovery_cfg = get_config().recovery
    thread_kwargs = dict(
        experiment_dir=exp_dir,
        output_dir=out_dir,
        ask=ask,
        trial_id=trial_id,
        data_dir=data_dir,
        data_path=str(get_paths().data_primary()),
        trial_code_dir=str(_trial_code_dir(out_dir)),
        legacy_trial_code_dir=str(_legacy_code_dir(out_dir)),
        trial_outputs_dir=str(_trial_outputs_dir(out_dir)),
        model_contract=_model_contract_prompt(),
        max_attempts=recovery_cfg.codex_max_attempts,
        retry_delay_s=float(recovery_cfg.retry_delay_seconds),
    )

    # ── 内部: T1~T4 全流程 ──
    def _run_trial_pipeline(cx: Codex) -> dict:
        """在已认证的 Codex session 上执行 T1→T2a→copy→T2b→T3→T4"""
        nonlocal manifest
        phase_order = [
            "evaluate", "plan", "copy_source", "codegen",
            "execute", "report", "feishu_card",
        ]

        def _phase_index(phase: str) -> int:
            try:
                return phase_order.index(phase)
            except ValueError:
                return len(phase_order)

        def _should_skip(phase: str) -> bool:
            if not resume or not manifest.is_completed(phase):
                return False
            if resume_from_phase is None:
                return True
            return _phase_index(phase) < _phase_index(resume_from_phase)

        def _completed_thread_id(phase: str) -> str | None:
            item = manifest.threads.get(phase, {})
            tid = item.get("thread_id")
            return str(tid) if tid else None

        def _stage_interrupted(phase: str, error: Exception) -> StageInterrupted:
            err = str(error)
            manifest.record_interrupted(phase, err)
            return StageInterrupted(trial_id, output_dir, phase, err)

        # ── T1: Evaluate + Diagnose ──
        try:
            if _should_skip("evaluate"):
                _stage_log(output_dir, "evaluate", "T1 skipped by completed manifest")
                raise StopIteration("skip:evaluate")
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
        except StopIteration:
            pass
        except Exception as e:
            if recovery_cfg.enabled and _is_request_timeout(e) and "evaluate" in recovery_cfg.recoverable_codex_phases:
                raise _stage_interrupted("evaluate", e)
            manifest.record_error("evaluate", str(e))
            raise

        # ── T2a: Plan ──
        try:
            if _should_skip("plan"):
                _stage_log(output_dir, "plan", "T2 plan skipped by completed manifest")
                raise StopIteration("skip:plan")
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
        except StopIteration:
            pass
        except Exception as e:
            if recovery_cfg.enabled and _is_request_timeout(e) and "plan" in recovery_cfg.recoverable_codex_phases:
                raise _stage_interrupted("plan", e)
            manifest.record_error("plan", str(e))
            raise

        # ── Copy Source (T2b 前置) ──
        manifest.start("copy_source", "python")
        _stage_log(output_dir, "copy_source", "T2 source copy started")
        copied = copy_source_to_trial(code_src_dir, output_dir)
        _normalize_trial_code_layout(output_dir, phase="copy-source")
        _write_data_refs(output_dir)
        _assert_candidate_code_clean(output_dir)
        print(f"\n[PRE-T2b] 已复制源码到 {get_paths().cfg.trial.code_dir}/: {len(copied)} 个文件 (来源: {code_src_dir})")
        _stage_log(output_dir, "copy_source", f"copied {len(copied)} files from {code_src_dir}")
        manifest.record("copy_source", "N/A (Python deterministic)", [
            get_paths().cfg.trial.code_dir,
            f"{get_paths().cfg.trial.inputs_dir}/data_refs.json",
            "logs/stage_copy_source.log",
        ])

        # ── T2b: Code Generation ──
        try:
            if _should_skip("codegen"):
                _stage_log(output_dir, "codegen", "T2 codegen skipped by completed manifest")
                t2b_id = _completed_thread_id("codegen") or ""
                raise StopIteration("skip:codegen")
            manifest.start("codegen", "codex")
            _stage_log(output_dir, "codegen", "T2 codegen started")
            t2b_id = run_codex_thread(cx, THREAD_CODEGEN, **thread_kwargs)
            _normalize_trial_code_layout(output_dir, phase="codegen")
            _assert_candidate_code_clean(output_dir)
            _require_artifacts(output_dir, [
                f"{get_paths().cfg.trial.code_dir}/train.py",
                "agent2/agent2_execution_plan.yaml",
            ], "codegen")
            manifest.record("codegen", t2b_id, [
                f"{get_paths().cfg.trial.code_dir}/train.py",
                "agent2/agent2_execution_plan.yaml",
                "logs/stage_codegen.log",
            ])
        except StopIteration:
            t2b_id = _completed_thread_id("codegen") or ""
            pass
        except Exception as e:
            if recovery_cfg.enabled and _is_request_timeout(e) and "codegen" in recovery_cfg.recoverable_codex_phases:
                raise _stage_interrupted("codegen", e)
            manifest.record_error("codegen", str(e))
            raise

        # ── T3 + codegen 回退循环 ──
        skip_execute = _should_skip("execute")
        if skip_execute:
            _stage_log(output_dir, "execute", "T3 skipped by completed manifest")
        else:
            manifest.start("execute", "python")
            _stage_log(output_dir, "execute", "T3 execute started")
        MAX_RETRIES = -1 if skip_execute else 2
        train_log_path = Path(output_dir) / "logs" / "train.log"
        comparison_path = Path(output_dir) / "evaluation" / "metric_comparison.json"
        comparison: dict | None = (
            json.loads(comparison_path.read_text(encoding="utf-8"))
            if skip_execute and comparison_path.exists()
            else None
        )
        for attempt in range(1 + MAX_RETRIES):
            try:
                _stage_log(output_dir, "execute", f"T3 attempt {attempt + 1}/{1 + MAX_RETRIES}")
                comparison = execute_t3(manifest, baseline_metric_dir=baseline_metric_dir)
            except Exception as e:
                _stage_log(output_dir, "execute", f"failed: {e}")
                manifest.record_error("execute", str(e))
                raise

            train_ok = not _needs_codegen_retry(comparison)
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
                fallback_template = _active_model_contract().get("default_train_command", [])
                if fallback_template:
                    print("[T3] 回退: 使用模型契约 default_train_command")
                    _stage_log(output_dir, "execute", "fallback default_train_command started")
                    fallback_cmd = _resolve_train_command(list(fallback_template), output, "fallback")
                    try:
                        fb_result = subprocess.run(
                            fallback_cmd,
                            cwd=str(_existing_code_dir(output_dir)),
                            capture_output=True, text=True, timeout=T3_TRAIN_TIMEOUT_SECONDS,
                        )
                    except subprocess.TimeoutExpired as e:
                        fb_timeout = f"fallback training timed out after {e.timeout} seconds"
                        _stage_log(output_dir, "execute", fb_timeout)
                        fb_result = subprocess.CompletedProcess(
                            args=fallback_cmd,
                            returncode=124,
                            stdout=_timeout_output_to_text(e.stdout),
                            stderr=(_timeout_output_to_text(e.stderr).rstrip() + f"\n{fb_timeout}").strip(),
                        )
                    _stage_log(output_dir, "execute", f"fallback training finished rc={fb_result.returncode}")
                    train_log_path.write_text(
                        (train_log_path.read_text(encoding="utf-8") if train_log_path.exists() else "") +
                        f"\n\n=== FALLBACK (model default_train_command) ===\n"
                        f"CMD: {' '.join(fallback_cmd)}\n"
                        f"RC: {fb_result.returncode}\n"
                        f"STDERR: {fb_result.stderr[-500:]}",
                        encoding="utf-8",
                    )
                    if fb_result.returncode == 0:
                        fb_pred = _resolve_contract_path(
                            output_contract.get("prediction_path", "new_prediction.csv"),
                            output,
                            "fallback",
                            default_base=_trial_outputs_dir(output),
                        )
                        if fb_pred.exists():
                            try:
                                fallback_wape, fallback_bias, fallback_rows = _compute_wape_bias_from_csv(fb_pred, split_filter, output_contract)
                                print(f"[T3] fallback {primary_level}: WAPE={fallback_wape:.4f}, Bias={fallback_bias:+.4f}")
                                _stage_log(output_dir, "execute", f"fallback WAPE={fallback_wape:.4f} Bias={fallback_bias:+.4f}")
                                primary = comparison.setdefault("primary", {})
                                primary["new_wape"] = fallback_wape
                                primary["new_bias"] = fallback_bias
                                wape_delta = round(primary.get("old_wape", 999) - fallback_wape, 6)
                                bias_delta = round(abs(fallback_bias) - abs(primary.get("old_bias", 0)), 6)
                                keep = wape_delta > 0.005 and bias_delta < 0.02
                                comparison["decision"] = "keep" if keep else "rollback"
                                comparison["wape_delta"] = wape_delta
                                comparison["bias_delta"] = bias_delta
                                comparison["reason"] = f"fallback default_train_command: WAPE delta={wape_delta:.4f}"
                                (Path(output_dir) / "evaluation" / "new_metrics.json").write_text(
                                    json.dumps({"wape": fallback_wape, "bias": fallback_bias, "rows": fallback_rows, "source": "fallback_default_train_command"}, indent=2),
                                    encoding="utf-8")
                            except Exception as fe:
                                print(f"[T3] 回退指标计算失败: {fe}")
                else:
                    _stage_log(output_dir, "execute", "fallback skipped: no model default_train_command configured")

                manifest.record("execute", "N/A (Python deterministic)", [
                    "agent2/run_status.json", "agent2/review_result.json",
                    "agent2/experiment_review.md",
                    "evaluation/metric_comparison.json", "logs/train.log",
                    "logs/stage_execute.log",
                ])
                print("[T3] 已达最大重试次数, 使用当前指标生成报告")
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

            failure_type = comparison.get("failure_type") if comparison else None
            eval_error = comparison.get("eval_error") if comparison else None
            retry_output_contract = _default_output_contract()
            try:
                import yaml

                exec_plan_path = output_dir / "agent2" / "agent2_execution_plan.yaml"
                exec_plan = yaml.safe_load(exec_plan_path.read_text(encoding="utf-8")) if exec_plan_path.exists() else {}
                retry_output_contract = _output_contract(exec_plan if isinstance(exec_plan, dict) else {})
            except Exception:
                retry_output_contract = _default_output_contract()
            error_text = (
                f"failure_type={failure_type}\n"
                f"eval_error={eval_error}\n\n"
                f"{error_text}\n\n"
                "Repair guidance:\n"
                f"- output_contract={json.dumps(retry_output_contract, ensure_ascii=False)}\n"
                "- Make train_command finish within the configured timeout on the full data reference.\n"
                "- Write the configured prediction_path with the configured split and metric columns.\n"
                "- Do not write the raw feature table as the evaluation output.\n"
            )

            fix_input = TextInput(text=(
                f"训练失败, 以下是错误日志:\n\n```\n{error_text}\n```\n\n"
                f"请定位错误原因, 修复 {get_paths().cfg.trial.code_dir}/ 下的代码, 然后更新 agent2_execution_plan.yaml。"
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
                train_py = _existing_code_dir(output_dir) / "train.py"
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
            if _should_skip("report"):
                _stage_log(output_dir, "report", "T4 report skipped by completed manifest")
                raise StopIteration("skip:report")
            manifest.start("report", "codex")
            _stage_log(output_dir, "report", "T4 report started")
            t4_id = run_codex_thread(cx, THREAD_REPORT, **thread_kwargs)
            manifest.record("report", t4_id, [
                "final_report.md",
                "logs/stage_report.log",
            ])
            cx.thread_archive(t4_id)
        except StopIteration:
            pass
        except Exception as e:
            err = str(e)
            _write_fallback_report(output_dir, err)
            manifest.record_degraded("report", err, [
                "final_report.md",
                "logs/stage_report.log",
            ])
            print(f"[T4] 报告生成失败, 已使用 fallback 报告继续: {err}")

        # ── T5: Feishu Review Card ──
        try:
            if _should_skip("feishu_card"):
                _stage_log(output_dir, "feishu-card", "T5 feishu card skipped by completed manifest")
                raise StopIteration("skip:feishu_card")
            manifest.start("feishu_card", "codex")
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
        except StopIteration:
            pass
        except Exception as e:
            err = str(e)
            _write_fallback_feishu_card(output_dir, err)
            manifest.record_degraded("feishu_card", err, [
                "feishu_review_card.md",
            ])
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
        ensure_codex_gateway()
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
    data_path: str | None = None,
) -> list[WorkflowManifest]:
    """
    循环执行多轮 Codex 优化实验。

    一次 Codex session 完成 N 轮 trial，每轮自动 --previous-trial 链式继承。
    额度不足时原地 sleep 等待恢复 (不重启 session)。
    停止条件: target_wape 达成 / WAPE 连续不改善 / max_iter 上限。

    Returns:
        每轮成功的 manifest 列表
    """
    if data_path:
        override_data_primary(data_path)

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

    ensure_codex_gateway()

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
                    data_path=data_path,
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
    parser.add_argument("--model-repo", default=None, help="Git MCP 模型仓库 repo_id (覆盖 mcp.git.active_repo)")
    parser.add_argument("--data-path", default=None, help="训练主数据 CSV 路径 (覆盖 flow_paths.yaml data.primary)")

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

    if args.model_repo:
        get_config().mcp.git.active_repo = args.model_repo

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
                data_path=args.data_path,
            )
            return 0

        # ── 单次模式 (兼容旧用法) ──
        run_workflow(
            experiment_dir=args.experiment,
            ask=args.ask,
            output_dir=args.output,
            previous_trial=args.previous_trial,
            data_path=args.data_path,
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
