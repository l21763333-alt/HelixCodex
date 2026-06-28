#!/usr/bin/env python3
"""
loop.py — 多轮实验人机协同编排器

═══════════════════════════════════════════════════════════════
  架构:  AIExperimentLoop
         ├── 驱动 codex_flow.run_workflow() (单轮实验)
         ├── 飞书通知 + 人审交互 (feishu_review)
         ├── Checkpoint 管理 (checkpoint_manager)
         └── keep / reverse / rollback 三态执行

  用法:
    python loop.py \
      --experiment baseline \
      --ask "分析预测误差，提出特征实验并验证" \
      --max-trials 10

  配置: flow_config.yaml → loop 段
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from config import get_config, get_paths, override_data_primary, reload_config
from lark_notify import (
    feishu_review,
    notify_loop_start,
    notify_loop_stop,
    notify_error,
    notify_command_result,
    notify_git_publish_result,
    notify_git_sync_result,
    notify_stage_interrupted,
    wait_for_recovery_event,
    human_review_enabled,
    normalize_supplement,
)
from checkpoint_manager import LoopCheckpointManager

try:
    from mcp_servers.lark_research_server.server import feishu_review_via_mcp
except Exception:  # pragma: no cover - optional adapter fallback
    feishu_review_via_mcp = None

try:
    from mcp_servers.git_research_server import server as baseline_git_mcp
except Exception:  # pragma: no cover - optional adapter fallback
    baseline_git_mcp = None

try:
    import git_subagent
except Exception:  # pragma: no cover - optional adapter fallback
    git_subagent = None


# ═══════════════════════════════════════════════════════════
# 主循环类
# ═══════════════════════════════════════════════════════════

class AIExperimentLoop:
    """
    多轮实验人机协同编排器。

    状态机:
        Round N 实验完成
           │
           ├── 自动决策: keep / rollback
           ├── 飞书通知 + 等待人工
           │     ├── keep     → checkpoint N = baseline, N+1
           │     ├── reverse  → restore N-1, 排除当前方向
           │     ├── rollback → 相同参数重试
           │     └── stop     → 结束
           └── 无人工 → 自动建议执行
    """

    def __init__(
        self,
        experiment_dir: str,
        ask: str,
        output_base: str = "runs",
        max_iter: int | None = None,
        target_wape: float | None = None,
        human_review: bool | None = None,
        review_timeout: int | None = None,
        model_repo: str | None = None,
        data_path: str | None = None,
    ):
        if data_path:
            override_data_primary(data_path)

        self.cfg = get_config()
        self.git_repo_id = model_repo or self.cfg.mcp.git.active_repo or "default"
        self.cfg.mcp.git.active_repo = self.git_repo_id
        loop_cfg = self.cfg.loop

        # ── 实验参数 ──
        self.experiment_dir = Path(experiment_dir)
        self.original_ask = ask
        self.data_path = data_path
        self.output_base = Path(output_base)
        self.output_base.mkdir(parents=True, exist_ok=True)
        self.run_label = self._make_run_label(self.output_base)

        # ── 循环控制 ──
        self.max_iter = max_iter if max_iter is not None else loop_cfg.max_iter
        self.target_wape = target_wape if target_wape is not None else loop_cfg.target_wape

        # ── 人审控制 ──
        if human_review is not None:
            loop_cfg.human_review.enabled = human_review
        if review_timeout is not None:
            loop_cfg.human_review.timeout = review_timeout

        # ── Checkpoint 管理器 ──
        self.ckpt = LoopCheckpointManager(runs_dir=self.output_base)

        # ── 运行时状态 ──
        self.round_num: int = 0
        self.trial_counter: int = self._detect_existing_trial_counter()
        self.source_experiment_dir: str = str(self.experiment_dir)
        self.current_previous_trial: str | None = None
        self.current_baseline_dir: str = str(self.experiment_dir)
        self.current_ask: str = self.original_ask
        self.human_supplements: list[str] = []
        self.should_stop: bool = False
        self.manifests: list[Any] = []
        self._trial_model_snapshots: dict[str, str] = {}
        self._restore_from_checkpoint_state()

    @staticmethod
    def _make_run_label(output_base: Path) -> str:
        label = output_base.name or "run"
        safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in label)
        return safe or "run"

    def _git_trial_id(self, trial_id: str) -> str:
        return f"{self.run_label}_{trial_id}"

    def _git_repo_cfg(self):
        return self.cfg.mcp.git.resolve_repo(self.git_repo_id)

    def _rollback_key(self) -> str:
        return f"round_{self.round_num:03d}"

    def _detect_existing_trial_counter(self) -> int:
        max_seen = 0
        for path in self.output_base.glob("trial_[0-9][0-9][0-9]"):
            try:
                max_seen = max(max_seen, int(path.name.rsplit("_", 1)[1]))
            except (IndexError, ValueError):
                continue
        return max_seen

    def _restore_from_checkpoint_state(self) -> None:
        """Resume loop counters and baseline from runs/.loop_state.json when present."""
        if self.ckpt.current_round <= 0 and not self.ckpt.current_baseline_trial:
            return

        self.round_num = max(self.round_num, self.ckpt.current_round)
        baseline_trial = self.ckpt.current_baseline_trial
        if baseline_trial:
            baseline_dir = self.output_base / baseline_trial
            if baseline_dir.exists():
                self.current_previous_trial = str(baseline_dir)
                self.current_baseline_dir = str(baseline_dir)
            else:
                print(f"[Loop] ⚠️ checkpoint baseline missing: {baseline_dir}")

        supplements = []
        for item in self.ckpt.lineage:
            supp = normalize_supplement(item.get("human_supplement"))
            if supp:
                supplements.append(supp)
        self.human_supplements = supplements
        if supplements:
            self.current_ask = self._augment_ask()

        print(
            f"[Loop] Resume checkpoint: round={self.round_num}, "
            f"baseline={baseline_trial or '(initial)'}, next_trial={self.trial_counter + 1:03d}"
        )

    # ── 主循环 ──────────────────────────────────────────

    def run(self) -> dict:
        """
        主循环入口。

        Returns:
            {
                "total_rounds": int,
                "best_round": dict | None,
                "final_wape": float,
                "stop_reason": str,
                "lineage": [...],
            }
        """
        self._on_loop_start()

        try:
            while not self.should_stop:
                # ── 收敛判断 ──
                if self._check_convergence():
                    break

                if self.round_num >= self.max_iter:
                    self._stop(f"达到最大轮次 {self.max_iter}")
                    break

                # ── 轮次递增 ──
                self.round_num += 1

                # ── 执行一轮 ──
                result = self._execute_one_round()

                if result is None:
                    # 实验执行异常
                    self._stop("实验执行异常")
                    break

                # ── 决策分支 ──
                decision = result["decision"]
                supplement = result.get("supplement")

                if decision == "keep":
                    self._on_keep(result)

                elif decision == "reverse":
                    self._on_reverse(result)

                elif decision == "rollback":
                    self._on_rollback(result)

                elif decision == "stop":
                    self._on_stop(result)
                    break

                # ── 目标达成判断 ──
                if self.target_wape is not None:
                    best = self.ckpt.get_best_round()
                    if best and best.get("wape", 999) <= self.target_wape:
                        self._stop(f"达成目标 WAPE ≤ {self.target_wape}")
                        break

        except KeyboardInterrupt:
            print("\n[Loop] 收到中断信号")
            self._stop("用户中断 (Ctrl+C)")

        except Exception as e:
            print(f"[Loop] 未捕获异常: {e}")
            import traceback
            traceback.print_exc()
            notify_error(str(e), f"Round {self.round_num}")
            self._stop(f"异常: {e}")

        return self._on_loop_end()

    # ── 单轮执行 ────────────────────────────────────────

    def _execute_one_round(self) -> dict | None:
        """执行一轮实验 + 飞书人审, 返回决策结果"""

        # 生成 trial ID
        self.trial_counter += 1
        trial_id = f"trial_{self.trial_counter:03d}"
        output_dir = str(self.output_base / trial_id)

        print(f"\n{'='*60}")
        print(f"[Loop] Round {self.round_num} → {trial_id}")
        print(f"[Loop] Experiment: {self.source_experiment_dir}")
        print(f"[Loop] Previous trial: {self.current_previous_trial or '(none)'}")
        print(f"[Loop] Ask: {self.current_ask[:200]}...")
        print(f"{'='*60}")

        model_snapshot_path: str | None = None
        git_trial_id = self._git_trial_id(trial_id)
        if self._git_mcp_enabled():
            try:
                if self.cfg.mcp.git.sync_before_each_trial and not self.current_previous_trial:
                    sync = self._sync_remote_base("before_trial")
                    if sync and not sync.get("ok", False):
                        return None
                branch_info = baseline_git_mcp.create_model_trial_branch(
                    git_trial_id,
                    repo_id=self.git_repo_id,
                )
                snapshot = baseline_git_mcp.snapshot_baseline_model(
                    git_trial_id,
                    repo_id=self.git_repo_id,
                )
                model_snapshot_path = snapshot["snapshot_path"]
                self._trial_model_snapshots[git_trial_id] = model_snapshot_path
                print(f"[Loop] Model branch: {branch_info['branch']}")
                print(f"[Loop] Model snapshot: {model_snapshot_path}")
            except Exception as e:
                print(f"[Loop] Git MCP baseline model setup failed: {e}")
                notify_error(str(e), f"Git MCP setup {git_trial_id}")
                return None

        # ── 调用 codex_flow.run_workflow ──
        try:
            import codex_flow as flow
            stage_interrupted_error = getattr(flow, "StageInterrupted", RuntimeError)
            resume_from_phase = None

            def _call_workflow(phase: str | None):
                kwargs = dict(
                    experiment_dir=self.source_experiment_dir,
                    ask=self.current_ask,
                    output_dir=output_dir,
                    previous_trial=self.current_previous_trial,
                )
                if self.data_path:
                    kwargs["data_path"] = self.data_path
                try:
                    return flow.run_workflow(
                        **kwargs,
                        resume=True,
                        resume_from_phase=phase,
                    )
                except TypeError as e:
                    if "unexpected keyword" not in str(e):
                        raise
                    kwargs.pop("data_path", None)
                    return flow.run_workflow(**kwargs)

            while True:
                try:
                    manifest = _call_workflow(resume_from_phase)
                    self.manifests.append(manifest)
                    break
                except stage_interrupted_error as interrupted:
                    print(f"[Loop] Stage interrupted: {interrupted.phase} — {interrupted.error}")
                    cmd = self._wait_for_stage_recovery(interrupted)
                    action = str(cmd.get("action", "stop"))
                    phase = str(cmd.get("phase") or interrupted.phase)
                    if action == "status":
                        notify_error(
                            f"Interrupted stage: {interrupted.phase}\n"
                            f"Action needed: /resume /retry-stage {interrupted.phase} "
                            f"/skip-stage {interrupted.phase} /stop",
                            f"Round {self.round_num} {trial_id}",
                        )
                        continue
                    if action == "stop":
                        return {
                            "decision": "stop",
                            "supplement": normalize_supplement(cmd.get("supplement")) or "用户停止阶段恢复",
                            "trial_id": trial_id,
                            "output_dir": output_dir,
                            "comparison": {},
                            "auto_decision": "stop",
                            "model_snapshot_path": model_snapshot_path,
                            "git_trial_id": git_trial_id,
                        }
                    if action == "skip-stage":
                        if self._skip_degraded_stage(interrupted, phase):
                            manifest = flow.WorkflowManifest.load(output_dir)
                            if manifest:
                                self.manifests.append(manifest)
                            break
                        continue
                    if action in {"resume", "retry-stage"}:
                        resume_from_phase = phase or interrupted.phase
                        continue
                    notify_error(f"unknown recovery action: {cmd}", f"Round {self.round_num} {trial_id}")
        except Exception as e:
            print(f"[Loop] 实验执行失败: {e}")
            notify_error(str(e), f"Round {self.round_num} {trial_id}")
            return None

        # ── 读取 comparison 结果 ──
        comparison_path = get_paths().trial_evaluation_dir(output_dir) / "metric_comparison.json"
        if not comparison_path.exists():
            print(f"[Loop] comparison 缺失: {comparison_path}")
            return None

        comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
        model_diff_summary = ""
        if self._git_mcp_enabled():
            try:
                diff = baseline_git_mcp.diff_trial_model_code(
                    get_paths().existing_trial_code_dir(output_dir),
                    repo_id=self.git_repo_id,
                )
                model_diff_summary = diff.get("summary", "")
                print(
                    f"[Loop] Model diff: {diff.get('changed', 0)} changed, "
                    f"{diff.get('added', 0)} added, {diff.get('removed', 0)} removed"
                )
            except Exception as e:
                model_diff_summary = f"Model diff unavailable: {e}"
                print(f"[Loop] Git MCP diff failed: {e}")

        # ── 自动决策 ──
        auto_decision = comparison.get("decision", "rollback")
        print(f"[Loop] 自动决策: {auto_decision.upper()}")

        # ── 飞书人审 ──
        human_decision = None
        human_approved = False
        if human_review_enabled():
            print(f"[Loop] 等待飞书人工审核...")
            if self._lark_mcp_enabled():
                human_decision, supplement = feishu_review_via_mcp(
                    trial_id=trial_id,
                    ask=self.current_ask,
                    comparison=comparison,
                    round_num=self.round_num,
                    auto_suggestion=auto_decision,
                    model_diff_summary=model_diff_summary,
                )
            else:
                human_decision, supplement = feishu_review(
                    trial_id=trial_id,
                    ask=self.current_ask,
                    comparison=comparison,
                    round_num=self.round_num,
                    auto_suggestion=auto_decision,
                )
            decision = human_decision or auto_decision
            human_approved = human_decision == "keep"
        else:
            decision = auto_decision
            supplement = None
            print(f"[Loop] 人审关闭, 直接使用自动建议")
        supplement = normalize_supplement(supplement)

        # ── 安全限制检查 ──
        decision = self._enforce_safety_limits(trial_id, decision)
        human_approved = bool(human_approved and decision == "keep")

        # ── 保存 checkpoint ──
        code_dir = get_paths().existing_trial_code_dir(output_dir)
        self.ckpt.save_round(
            trial_id=trial_id,
            round_num=self.round_num,
            decision=decision,
            comparison=comparison,
            code_dir=code_dir,
            ask=self.current_ask,
            human_supplement=supplement,
            parent_round=self._get_parent_round(decision),
            rollback_key=self._rollback_key(),
        )

        # ── 通知决策结果 ──
        next_round = self.round_num if decision == "rollback" else self.round_num + 1
        notify_command_result(decision, supplement, next_round)

        return {
            "decision": decision,
            "supplement": supplement,
            "trial_id": trial_id,
            "output_dir": output_dir,
            "comparison": comparison,
            "auto_decision": auto_decision,
            "human_approved": human_approved,
            "model_snapshot_path": model_snapshot_path,
            "git_trial_id": git_trial_id,
        }

    # ── 三态处理 ────────────────────────────────────────

    def _on_keep(self, result: dict) -> None:
        """keep: 保留当前版本, 继续前进"""
        print(f"[Loop] ✅ KEEP — {result['trial_id']} 成为新基线")

        if self._git_mcp_enabled():
            try:
                trial_code_dir = get_paths().existing_trial_code_dir(result["output_dir"])
                git_trial_id = result.get("git_trial_id", result["trial_id"])
                baseline_git_mcp.apply_trial_to_baseline(
                    trial_code_dir,
                    git_trial_id,
                    repo_id=self.git_repo_id,
                )
                commit = baseline_git_mcp.commit_baseline_model_update(
                    git_trial_id,
                    result.get("comparison", {}),
                    str(Path(result["output_dir"]) / "final_report.md"),
                    result.get("supplement"),
                    repo_id=self.git_repo_id,
                )
                print(f"[Loop] Baseline model Git commit: {commit}")
                self._publish_committed_keep(result, commit)
            except Exception as e:
                print(f"[Loop] Git MCP keep commit failed: {e}")
                notify_error(str(e), f"Git MCP keep {result['trial_id']}")

        # 更新基线
        self.current_previous_trial = result["output_dir"]
        self.current_baseline_dir = result["output_dir"]

        # 注入人工补充到下一轮 Ask
        supplement = result.get("supplement")
        supplement = normalize_supplement(supplement)
        if supplement:
            self.human_supplements.append(supplement)
            self.current_ask = self._augment_ask(supplement)
            print(f"[Loop] 人工补充已注入: {supplement[:100]}...")

        self.round_num = self.ckpt.current_round  # 同步

    def _on_reverse(self, result: dict) -> None:
        """reverse: 放弃当前方向, 恢复到上一轮"""
        print(f"[Loop] ⏪ REVERSE — 放弃 {result['trial_id']}")

        # 找到要恢复到的轮次
        target_round = self._find_reverse_target()
        print(f"[Loop] 恢复到 Round {target_round}")

        restored = None
        if target_round > 0:
            try:
                restored = self.ckpt.restore_round(target_round)
            except (ValueError, FileNotFoundError) as e:
                print(f"[Loop] 恢复失败: {e}")
                notify_error(str(e), f"reverse 到 Round {target_round}")
                self._stop(f"无法恢复到 Round {target_round}")
                return

        if self._git_mcp_enabled():
            try:
                snapshot_path = (
                    result.get("model_snapshot_path")
                    or self._trial_model_snapshots.get(result.get("git_trial_id", result["trial_id"]))
                )
                git_trial_id = result.get("git_trial_id", result["trial_id"])
                if snapshot_path:
                    baseline_git_mcp.restore_baseline_model_snapshot(
                        snapshot_path,
                        git_trial_id,
                        repo_id=self.git_repo_id,
                    )
                baseline_git_mcp.discard_unaccepted_model_changes(
                    git_trial_id,
                    repo_id=self.git_repo_id,
                )
            except Exception as e:
                print(f"[Loop] Git MCP reverse restore failed: {e}")
                notify_error(str(e), f"Git MCP reverse {result['trial_id']}")

        # 恢复基线
        if restored:
            restored_trial_dir = self.output_base / restored["trial_id"]
            if restored_trial_dir.exists():
                self.current_previous_trial = str(restored_trial_dir)
                self.current_baseline_dir = str(restored_trial_dir)
            else:
                self.current_previous_trial = None
                self.current_baseline_dir = self.source_experiment_dir
        else:
            self.current_previous_trial = None
            self.current_baseline_dir = self.source_experiment_dir
        self.round_num = target_round

        # 注入人工补充 + 排除方向
        supplement = result.get("supplement")
        supplement = normalize_supplement(supplement)
        if supplement:
            self.human_supplements.append(supplement)

        # 重建 Ask (排除已知无效方向)
        self.current_ask = self._augment_ask(
            supplement,
            include_excluded=True,
        )
        restored_name = restored["trial_id"] if restored else "initial baseline"
        print(f"[Loop] 新基线: Round {target_round}, {restored_name}")

    def _on_rollback(self, result: dict) -> None:
        """rollback: 重跑本轮 (相同参数, 不改变基线)"""
        trial_id = result["trial_id"]
        count = self.ckpt._state.rollback_count.get(self._rollback_key(), 0)
        print(f"[Loop] 🔄 ROLLBACK — 重跑 {trial_id} (第 {count} 次重试)")

        # 不改变基线、不递增 round_num
        # round_num 不变, 下一轮 _execute_one_round 会用相同的 round_num
        # 但 trial_id 会递增 (trial_counter 已增加)
        self.round_num = max(0, self.round_num - 1)

        supplement = result.get("supplement")
        supplement = normalize_supplement(supplement)
        if supplement:
            self.current_ask = self._augment_ask(supplement)
            self.human_supplements.append(supplement)

        if self._git_mcp_enabled():
            try:
                baseline_git_mcp.discard_unaccepted_model_changes(
                    result.get("git_trial_id", trial_id),
                    repo_id=self.git_repo_id,
                )
            except Exception as e:
                print(f"[Loop] Git MCP rollback discard failed: {e}")
                notify_error(str(e), f"Git MCP rollback {trial_id}")

    def _on_stop(self, result: dict) -> None:
        """stop: 结束循环"""
        reason = normalize_supplement(result.get("supplement")) or "用户指令"
        self._stop(reason)

    # ── 辅助方法 ────────────────────────────────────────

    def _get_parent_round(self, decision: str) -> int | None:
        """计算当前轮次的 parent_round"""
        if decision == "keep":
            # parent 是上一个 keep 的轮次
            kept = self.ckpt.get_keep_chain()
            return kept[-1]["round"] if kept else None
        elif decision == "reverse":
            return self._find_reverse_target()
        else:
            # rollback: parent 不变
            return self.ckpt.current_round if self.ckpt.current_round > 0 else None

    def _wait_for_stage_recovery(self, interrupted) -> dict:
        """Send a recovery card and wait for a Feishu recovery command."""
        trial_id = getattr(interrupted, "trial_id", "")
        phase = getattr(interrupted, "phase", "")
        error = getattr(interrupted, "error", str(interrupted))
        if not human_review_enabled():
            return {"action": "stop", "phase": phase, "supplement": "stage recovery requires Feishu review"}

        notify_stage_interrupted(trial_id, phase, error)
        recovery_cfg = self.cfg.recovery
        cmd = wait_for_recovery_event(
            self.cfg.feishu.chat_id,
            trial_id,
            phase,
            timeout=recovery_cfg.manual_timeout,
            poll_interval=self.cfg.feishu.poll_interval,
            sender_filter=self.cfg.loop.human_review.authorized_senders or None,
        )
        if not cmd:
            return {"action": "stop", "phase": phase, "supplement": "stage recovery timed out"}
        return cmd

    def _skip_degraded_stage(self, interrupted, phase: str) -> bool:
        """Mark a degradable stage as skipped/degraded so the round can continue."""
        if phase not in self.cfg.recovery.degradable_phases:
            notify_error(f"stage {phase} is not degradable", f"Recovery {getattr(interrupted, 'trial_id', '')}")
            return False
        try:
            import codex_flow as flow
            output_dir = getattr(interrupted, "output_dir")
            error = getattr(interrupted, "error", "manual skip")
            manifest = flow.WorkflowManifest.load(output_dir)
            if phase == "report":
                flow._write_fallback_report(output_dir, f"manual skip after interruption: {error}")
                if manifest:
                    manifest.record_degraded("report", f"manual skip: {error}", [
                        "final_report.md",
                        "logs/stage_report.log",
                    ])
            elif phase == "feishu_card":
                flow._write_fallback_feishu_card(output_dir, f"manual skip after interruption: {error}")
                if manifest:
                    manifest.record_degraded("feishu_card", f"manual skip: {error}", [
                        "feishu_review_card.md",
                    ])
            return True
        except Exception as e:
            notify_error(str(e), f"skip-stage {phase}")
            return False

    def _sync_remote_base(self, context: str = "loop_start") -> dict | None:
        """Synchronize baseline model from the configured remote base branch."""
        if not self._git_mcp_enabled():
            return None
        if self.current_previous_trial:
            result = {
                "ok": True,
                "sync": {
                    "synced": False,
                    "reason": "skip sync while resuming an existing keep chain",
                    "remote": self._git_repo_cfg().remote,
                    "base_branch": self._git_repo_cfg().base_branch,
                },
            }
            notify_git_sync_result(result)
            return result
        repo_cfg = self._git_repo_cfg()
        try:
            if self.cfg.mcp.git.publish_via_subagent and git_subagent is not None:
                try:
                    result = git_subagent.sync_remote_base_via_subagent(repo_id=self.git_repo_id)
                except Exception as subagent_error:
                    if not hasattr(baseline_git_mcp, "sync_remote_base"):
                        raise
                    sync = baseline_git_mcp.sync_remote_base(
                        repo_cfg.remote,
                        repo_cfg.base_branch,
                        repo_id=self.git_repo_id,
                    )
                    result = {
                        "ok": bool(sync.get("synced")) and not sync.get("blocked"),
                        "sync": sync,
                        "fallback": "direct_git_mcp",
                        "subagent_error": str(subagent_error),
                    }
            else:
                sync = baseline_git_mcp.sync_remote_base(
                    repo_cfg.remote,
                    repo_cfg.base_branch,
                    repo_id=self.git_repo_id,
                )
                result = {"ok": bool(sync.get("synced")) and not sync.get("blocked"), "sync": sync}
        except Exception as e:
            result = {"ok": False, "sync": {}, "error": str(e), "context": context}
        notify_git_sync_result(result)
        if not result.get("ok", False):
            self._stop(f"Git baseline sync failed: {result.get('error') or result.get('sync', {}).get('reason')}")
        return result

    def _publish_committed_keep(self, result: dict, commit: dict) -> None:
        """Push KEEP commit and create a draft PR after the checkpoint is saved."""
        if not commit.get("committed"):
            notify_git_publish_result(
                {
                    "ok": True,
                    "publish": {
                        "trial_id": result.get("trial_id"),
                        "branch": None,
                        "commit": commit,
                        "push": None,
                        "pr": None,
                    },
                },
                next_baseline=result.get("output_dir"),
            )
            return

        git_trial_id = result.get("git_trial_id", result["trial_id"])
        repo_cfg = self._git_repo_cfg()
        branch = f"{repo_cfg.branch_prefix}{git_trial_id}"
        report_path = str(Path(result["output_dir"]) / "final_report.md")
        result_path = Path(result["output_dir"]) / "git_publish_result.json"
        try:
            if self.cfg.mcp.git.publish_via_subagent and git_subagent is not None:
                try:
                    publish = git_subagent.publish_existing_keep_via_subagent(
                        trial_id=git_trial_id,
                        branch=branch,
                        metrics=result.get("comparison", {}),
                        report_path=report_path,
                        supplement=result.get("supplement"),
                        commit=commit,
                        result_path=result_path,
                        repo_id=self.git_repo_id,
                        human_approved=bool(result.get("human_approved")),
                    )
                except Exception as subagent_error:
                    push = None
                    pr = None
                    if repo_cfg.push_on_keep:
                        push = baseline_git_mcp.push_model_trial_branch(
                            branch=branch,
                            remote=repo_cfg.remote,
                            target_branch=repo_cfg.push_target_branch or None,
                            repo_id=self.git_repo_id,
                            human_approved=bool(result.get("human_approved")),
                        )
                    if repo_cfg.create_pr_on_keep:
                        if self.cfg.mcp.git.require_human_approval_for_push and not result.get("human_approved"):
                            pr = {"created": False, "blocked": True, "reason": "remote PR creation requires an explicit human KEEP approval"}
                        else:
                            pr = baseline_git_mcp.create_model_pr(
                                branch=branch,
                                base=repo_cfg.base_branch,
                                body=json.dumps(result.get("comparison", {}), indent=2, ensure_ascii=False),
                                title=f"forecast: keep {git_trial_id}",
                                draft=repo_cfg.pr_draft,
                                repo_id=self.git_repo_id,
                            )
                    publish = {
                        "ok": True,
                        "operation": "publish_existing_keep",
                        "fallback": "direct_git_mcp",
                        "subagent_error": str(subagent_error),
                        "publish": {
                            "trial_id": git_trial_id,
                            "branch": branch,
                            "commit": commit,
                            "push": push,
                            "pr": pr,
                        },
                        "state": baseline_git_mcp.get_model_repo_state(repo_id=self.git_repo_id),
                        "error": None,
                    }
                    result_path.write_text(json.dumps(publish, indent=2, ensure_ascii=False), encoding="utf-8")
            else:
                push = None
                pr = None
                if repo_cfg.push_on_keep:
                    push = baseline_git_mcp.push_model_trial_branch(
                        branch=branch,
                        remote=repo_cfg.remote,
                        target_branch=repo_cfg.push_target_branch or None,
                        repo_id=self.git_repo_id,
                        human_approved=bool(result.get("human_approved")),
                    )
                if repo_cfg.create_pr_on_keep:
                    if self.cfg.mcp.git.require_human_approval_for_push and not result.get("human_approved"):
                        pr = {"created": False, "blocked": True, "reason": "remote PR creation requires an explicit human KEEP approval"}
                    else:
                        pr = baseline_git_mcp.create_model_pr(
                            branch=branch,
                            base=repo_cfg.base_branch,
                            body=json.dumps(result.get("comparison", {}), indent=2, ensure_ascii=False),
                            title=f"forecast: keep {git_trial_id}",
                            draft=repo_cfg.pr_draft,
                            repo_id=self.git_repo_id,
                        )
                publish = {
                    "ok": True,
                    "operation": "publish_existing_keep",
                    "publish": {
                        "trial_id": git_trial_id,
                        "branch": branch,
                        "commit": commit,
                        "push": push,
                        "pr": pr,
                    },
                    "state": baseline_git_mcp.get_model_repo_state(repo_id=self.git_repo_id),
                    "error": None,
                }
                result_path.write_text(json.dumps(publish, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            publish = {
                "ok": False,
                "operation": "publish_existing_keep",
                "publish": {
                    "trial_id": git_trial_id,
                    "branch": branch,
                    "commit": commit,
                    "push": None,
                    "pr": None,
                },
                "error": str(e),
            }
            result_path.write_text(json.dumps(publish, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[Loop] Git publish failed: {e}")
            notify_error(str(e), f"Git publish {result['trial_id']}")
        notify_git_publish_result(publish, next_baseline=result.get("output_dir"))

    def _lark_mcp_enabled(self) -> bool:
        return bool(
            getattr(self.cfg, "mcp", None)
            and self.cfg.mcp.lark.enabled
            and feishu_review_via_mcp is not None
        )

    def _git_mcp_enabled(self) -> bool:
        return bool(
            getattr(self.cfg, "mcp", None)
            and self.cfg.mcp.git.enabled
            and self.cfg.mcp.git.scope == "baseline_model"
            and baseline_git_mcp is not None
        )

    def _find_reverse_target(self) -> int:
        """找到 reverse 的目标轮次 (最近一个 keep)"""
        kept = self.ckpt.get_keep_chain()
        if len(kept) >= 2:
            return kept[-2]["round"]  # 上上一个 keep
        elif len(kept) == 1:
            return 0  # 回到初始基线
        return 0

    def _enforce_safety_limits(self, trial_id: str, decision: str) -> str:
        """强制执行安全限制"""
        limits = self.cfg.loop.limits

        if decision == "rollback":
            if self.ckpt.should_force_rollback_to_reverse(
                self._rollback_key(), limits.max_rollbacks_per_round
            ):
                print(
                    f"[Loop] ⚠️ rollback 次数已达上限 "
                    f"({limits.max_rollbacks_per_round}), 强制降级为 reverse"
                )
                return "reverse"

        if decision == "reverse":
            if not self.ckpt.can_reverse(limits.max_consecutive_reverses):
                print(
                    f"[Loop] ⚠️ 连续 reverse 已达上限 "
                    f"({limits.max_consecutive_reverses}), 强制停止"
                )
                self._stop("连续 reverse 超限")
                return "stop"

        return decision

    def _augment_ask(self, supplement: str | None = None,
                     include_excluded: bool = False) -> str:
        """增强 Ask — 注入人工补充 + 排除方向"""
        parts = [self.original_ask]

        # 最新人工补充
        supplement = normalize_supplement(supplement)
        if supplement:
            parts.append(f"\n💬 人工补充: {supplement}")

        # 排除方向
        if include_excluded:
            excluded = self.ckpt.get_excluded_summary()
            if excluded:
                parts.append(excluded)
                parts.append("请避免以上方向, 尝试新的优化策略。")

        # 历史补充 (最近 3 条)
        if self.human_supplements:
            recent = self.human_supplements[-3:]
            if len(recent) > 0 and (not supplement or supplement not in recent):
                parts.append(f"\n历史补充: {'; '.join(recent)}")

        return "\n".join(parts)

    def _check_convergence(self) -> bool:
        """收敛检查"""
        cv = self.cfg.loop.convergence

        if self.ckpt.should_converge(
            cv.min_wape_improvement,
            cv.max_rounds_without_improvement,
        ):
            rounds = self.ckpt.rounds_without_improvement(cv.min_wape_improvement)
            self._stop(
                f"连续 {rounds} 轮 WAPE 改善不足 "
                f"(< {cv.min_wape_improvement})"
            )
            return True
        return False

    def _stop(self, reason: str) -> None:
        """标记停止"""
        if not self.should_stop:
            print(f"\n[Loop] 🛑 停止: {reason}")
            self.should_stop = True
            self._stop_reason = reason

    def _on_loop_start(self) -> None:
        """循环启动通知"""
        print(f"\n{'='*60}")
        print(f"[Loop] 🚀 Codex Flow 多轮实验启动")
        print(f"[Loop] 实验目录: {self.experiment_dir}")
        print(f"[Loop] 目标: {self.original_ask}")
        print(f"[Loop] 最大轮次: {self.max_iter}")
        print(f"[Loop] 人工审核: {'✅' if human_review_enabled() else '❌'}")
        if self._git_mcp_enabled():
            print(f"[Loop] Git MCP repo: {self.git_repo_id}")
        print(f"{'='*60}")

        hr_enabled = human_review_enabled()
        notify_loop_start(
            experiment=str(self.experiment_dir),
            ask=self.original_ask,
            max_iter=self.max_iter,
            target_wape=self.target_wape,
            model=self.cfg.model,
            human_review=hr_enabled,
        )

        if self._git_mcp_enabled() and self.cfg.mcp.git.sync_on_loop_start:
            self._sync_remote_base("loop_start")
            if self.should_stop:
                return

        # 初始化 checkpoint
        if self.ckpt.current_round == 0:
            self.round_num = 0
        else:
            self.round_num = self.ckpt.current_round
            if self.ckpt.current_baseline_trial:
                baseline_trial_dir = self.output_base / self.ckpt.current_baseline_trial
                if baseline_trial_dir.exists():
                    self.current_previous_trial = str(baseline_trial_dir)
                    self.current_baseline_dir = str(baseline_trial_dir)
            print(f"[Loop] 从 checkpoint 恢复: Round {self.round_num}")

    def _on_loop_end(self) -> dict:
        """循环结束汇总"""
        best = self.ckpt.get_best_round()
        final_wape = best.get("wape", 999) if best else 999

        reason = getattr(self, "_stop_reason", "正常结束")

        print(f"\n{'='*60}")
        print(f"[Loop] 循环结束")
        print(f"[Loop] 总轮次: {self.round_num}")
        print(f"[Loop] 停止原因: {reason}")
        print(f"[Loop] 最优 WAPE: {final_wape:.4f}" if best else "[Loop] 无有效结果")
        print(f"[Loop] 追溯链:")
        print(self.ckpt.get_lineage_tree())
        print(f"{'='*60}")

        # 飞书通知
        notify_loop_stop(reason, self.manifests)

        return {
            "total_rounds": self.round_num,
            "best_round": best,
            "final_wape": final_wape,
            "stop_reason": reason,
            "lineage": self.ckpt.lineage,
        }


# ═══════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Codex Flow 多轮实验编排器 (人机协同)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python loop.py --experiment baseline --ask "分析预测误差，提出特征实验并验证"
  python loop.py --experiment baseline --ask "优化WAPE" --max-trials 5 --no-review
  python loop.py --experiment runs/trial_029 --ask "继续优化" --review-timeout 600
        """,
    )
    parser.add_argument(
        "--experiment", required=True,
        help="实验目录 (baseline/ 或上一轮 trial 路径)",
    )
    parser.add_argument(
        "--ask", required=True,
        help="实验目标描述",
    )
    parser.add_argument(
        "--output", default="runs",
        help="输出根目录 (默认: runs)",
    )
    parser.add_argument(
        "--max-trials", type=int, default=None,
        help="最大实验轮次 (覆盖 flow_config.yaml)",
    )
    parser.add_argument(
        "--target-wape", type=float, default=None,
        help="目标 WAPE, 达成后自动停止",
    )
    parser.add_argument(
        "--no-review", action="store_true",
        help="关闭飞书人工审核 (全自动模式)",
    )
    parser.add_argument(
        "--review-timeout", type=int, default=None,
        help="人工审核超时秒数 (覆盖 flow_config.yaml)",
    )
    parser.add_argument(
        "--model-repo", type=str, default=None,
        help="Git MCP 模型仓库 repo_id (覆盖 mcp.git.active_repo)",
    )
    parser.add_argument(
        "--data-path", type=str, default=None,
        help="训练主数据 CSV 路径 (覆盖 flow_paths.yaml data.primary)",
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="指定 YAML 配置文件路径",
    )

    args = parser.parse_args()

    # 加载指定配置
    if args.config:
        reload_config(path=args.config)

    # 创建循环
    loop = AIExperimentLoop(
        experiment_dir=args.experiment,
        ask=args.ask,
        output_base=args.output,
        max_iter=args.max_trials,
        target_wape=args.target_wape,
        human_review=not args.no_review,
        review_timeout=args.review_timeout,
        model_repo=args.model_repo,
        data_path=args.data_path,
    )

    # 执行
    result = loop.run()

    # 输出结果
    print(f"\n{'='*60}")
    print("最终结果:")
    print(json.dumps({
        "total_rounds": result["total_rounds"],
        "final_wape": result["final_wape"],
        "stop_reason": result["stop_reason"],
    }, indent=2, ensure_ascii=False))

    return 0 if result["stop_reason"] not in ("异常",) else 1


if __name__ == "__main__":
    sys.exit(main())
