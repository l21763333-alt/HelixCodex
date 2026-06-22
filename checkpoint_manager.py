#!/usr/bin/env python3
"""
checkpoint_manager.py — 多轮实验状态管理 + 追溯链维护

职责:
  1. 每轮实验 save checkpoint (代码 + 参数 + 指标)
  2. reverse 时 restore checkpoint (恢复到指定轮次)
  3. 维护 lineage 追溯链 (谁从谁 fork)
  4. 管理排除列表 (哪些方向已证明无效)
  5. 安全限制: rollback 计数 / 连续 reverse 检测

数据存储:
  runs/.loop_state.json         ← 全局循环状态 (lineage + 排除列表)
  runs/trial_NNN/.checkpoint/   ← 单轮快照 (代码 + 参数 + 指标)

不依赖飞书或 Codex — 纯文件系统操作。
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path


# ═══════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════

@dataclass
class LoopState:
    """全局循环状态, 序列化到 runs/.loop_state.json"""
    current_round: int = 0
    current_baseline_trial: str = ""   # 当前基线 trial
    lineage: list[dict] = field(default_factory=list)
    excluded_directions: list[dict] = field(default_factory=list)
    rollback_count: dict[str, int] = field(default_factory=dict)  # trial_id → 重试次数
    consecutive_reverses: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "current_round": self.current_round,
            "current_baseline_trial": self.current_baseline_trial,
            "lineage": self.lineage,
            "excluded_directions": self.excluded_directions,
            "rollback_count": self.rollback_count,
            "consecutive_reverses": self.consecutive_reverses,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LoopState":
        return cls(
            current_round=d.get("current_round", 0),
            current_baseline_trial=d.get("current_baseline_trial", ""),
            lineage=d.get("lineage", []),
            excluded_directions=d.get("excluded_directions", []),
            rollback_count=d.get("rollback_count", {}),
            consecutive_reverses=d.get("consecutive_reverses", 0),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )


# ═══════════════════════════════════════════════════════════
# Checkpoint 管理器
# ═══════════════════════════════════════════════════════════

class LoopCheckpointManager:
    """
    管理跨 trial 的实验状态。

    用法:
        mgr = LoopCheckpointManager(runs_dir=Path("runs"))
        mgr.save_round(...)     # 一轮实验完成后
        mgr.restore_round(3)    # reverse 时恢复
        mgr.can_rollback(trial) # 检查是否还能重试
        mgr.can_reverse()       # 检查是否还能回溯
    """

    def __init__(self, runs_dir: Path):
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.runs_dir / ".loop_state.json"
        self._state: LoopState = self._load()

    # ── 持久化 ──────────────────────────────────────────

    def _load(self) -> LoopState:
        """从磁盘加载循环状态"""
        if self.state_file.exists():
            try:
                raw = json.loads(self.state_file.read_text(encoding="utf-8"))
                return LoopState.from_dict(raw)
            except (json.JSONDecodeError, KeyError):
                print("[Checkpoint] 状态文件损坏, 使用全新状态")
        return LoopState()

    def _save(self) -> None:
        """持久化循环状态到磁盘"""
        self._state.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.state_file.write_text(
            json.dumps(self._state.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ── 属性 ────────────────────────────────────────────

    @property
    def current_round(self) -> int:
        return self._state.current_round

    @property
    def current_baseline_trial(self) -> str:
        return self._state.current_baseline_trial

    @property
    def lineage(self) -> list[dict]:
        return list(self._state.lineage)

    @property
    def excluded_directions(self) -> list[dict]:
        return list(self._state.excluded_directions)

    # ── 轮次操作 ────────────────────────────────────────

    def save_round(
        self,
        trial_id: str,
        round_num: int,
        decision: str,
        comparison: dict,
        code_dir: Path,
        ask: str,
        human_supplement: str | None = None,
        parent_round: int | None = None,
    ) -> None:
        """
        保存一轮实验的完整快照。

        Args:
            trial_id: 实验 ID ("trial_029")
            round_num: 轮次号
            decision: "keep" | "reverse" | "rollback"
            comparison: metric_comparison dict
            code_dir: agent2/code/ 目录路径 (备份此目录)
            ask: 本轮使用的 Ask 文本
            human_supplement: 人工补充文本
            parent_round: 父轮次 (keep 时 = round_num-1, reverse 时 = 上一 keep)
        """
        primary = comparison.get("primary", {})

        # ── 备份代码目录 ──
        ckpt_dir = self.runs_dir / trial_id / ".checkpoint"
        if ckpt_dir.exists():
            shutil.rmtree(ckpt_dir)
        ckpt_dir.mkdir(parents=True, exist_ok=True)

        # 跳过这些目录/文件, 避免递归复制自身
        _SKIP = {".checkpoint", "__pycache__", ".git", "node_modules",
                 ".loop_state.json", "data", "outputs", "logs"}

        if code_dir.exists() and code_dir != ckpt_dir:
            for item in code_dir.iterdir():
                if item.name in _SKIP:
                    continue
                dest = ckpt_dir / item.name
                try:
                    if item.is_dir():
                        shutil.copytree(item, dest)
                    else:
                        shutil.copy2(item, dest)
                except (OSError, shutil.Error):
                    pass  # 跳过无法复制的文件

        # ── 保存实验快照元数据 ──
        snapshot = {
            "trial_id": trial_id,
            "round_num": round_num,
            "decision": decision,
            "parent_round": parent_round,
            "ask": ask,
            "human_supplement": human_supplement,
            "wape": primary.get("new_wape", 999),
            "wape_delta": comparison.get("wape_delta", 0),
            "bias": primary.get("new_bias", 0),
            "bias_delta": comparison.get("bias_delta", 0),
            "reason": comparison.get("reason", ""),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        (ckpt_dir / "snapshot.json").write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # ── 更新 lineage ──
        self._state.lineage.append({
            "round": round_num,
            "trial": trial_id,
            "decision": decision,
            "parent": parent_round,
            "wape": primary.get("new_wape", 999),
            "wape_delta": comparison.get("wape_delta", 0),
            "human_supplement": human_supplement,
            "timestamp": snapshot["timestamp"],
        })

        # ── 更新状态 ──
        if decision == "keep":
            self._state.current_baseline_trial = trial_id
            self._state.current_round = round_num
            self._state.consecutive_reverses = 0
            # 清除当前 trial 的 rollback 计数
            self._state.rollback_count.pop(trial_id, None)

        elif decision == "reverse":
            self._state.excluded_directions.append({
                "trial": trial_id,
                "round": round_num,
                "wape_delta": comparison.get("wape_delta", 0),
                "reason": f"reverse: {comparison.get('reason', 'N/A')}",
            })
            self._state.consecutive_reverses += 1

        elif decision == "rollback":
            self._state.rollback_count[trial_id] = \
                self._state.rollback_count.get(trial_id, 0) + 1

        self._save()
        print(f"[Checkpoint] Round {round_num} ({decision}) 已保存 → {ckpt_dir}")

    def restore_round(self, round_num: int) -> dict:
        """
        恢复到指定轮次的状态。

        Args:
            round_num: 要恢复到的轮次

        Returns:
            {
                "trial_id": str,
                "round": int,
                "code_dir": Path,       ← 代码备份目录
                "wape": float,
                "ask": str,
            }

        Raises:
            ValueError: 轮次不存在
            FileNotFoundError: checkpoint 目录缺失
        """
        # 在 lineage 中查找
        entry = None
        for e in self._state.lineage:
            if e["round"] == round_num and e["decision"] == "keep":
                entry = e
                break
        if entry is None:
            # 回退: 找 ≤ round_num 的最近一个 keep
            for e in reversed(self._state.lineage):
                if e["round"] <= round_num and e["decision"] == "keep":
                    entry = e
                    break
        if entry is None:
            raise ValueError(
                f"无法恢复到 Round {round_num}: lineage 中无 keep 记录"
            )

        ckpt_dir = self.runs_dir / entry["trial"] / ".checkpoint"
        if not ckpt_dir.exists():
            raise FileNotFoundError(f"Checkpoint 缺失: {ckpt_dir}")

        # 读取 snapshot
        snapshot_path = ckpt_dir / "snapshot.json"
        snapshot = {}
        if snapshot_path.exists():
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

        return {
            "trial_id": entry["trial"],
            "round": round_num,
            "code_dir": ckpt_dir,
            "wape": entry.get("wape", 999),
            "ask": snapshot.get("ask", ""),
            "snapshot": snapshot,
        }

    def get_best_round(self) -> dict | None:
        """获取历史最优轮次 (按 WAPE)"""
        kept = [
            e for e in self._state.lineage
            if e["decision"] == "keep" and e.get("wape", 999) < 999
        ]
        if not kept:
            return None
        return min(kept, key=lambda e: e["wape"])

    # ── 安全限制检查 ────────────────────────────────────

    def can_rollback(self, trial_id: str, max_rollbacks: int = 3) -> bool:
        """检查是否还能 rollback (未超上限)"""
        count = self._state.rollback_count.get(trial_id, 0)
        return count < max_rollbacks

    def can_reverse(self, max_consecutive: int = 2) -> bool:
        """检查是否还能 reverse (连续 reverse 未超上限)"""
        return self._state.consecutive_reverses < max_consecutive

    def should_force_rollback_to_reverse(self, trial_id: str,
                                         max_rollbacks: int = 3) -> bool:
        """rollback 次数超限 → 应强制降级为 reverse"""
        return not self.can_rollback(trial_id, max_rollbacks)

    # ── 收敛检查 ────────────────────────────────────────

    def rounds_without_improvement(self, threshold: float = 0.005) -> int:
        """统计连续无显著改善的轮次数"""
        count = 0
        for e in reversed(self._state.lineage):
            if e["decision"] == "keep" and abs(e.get("wape_delta", 0)) < threshold:
                count += 1
            elif e["decision"] == "keep":
                break  # 有显著改善, 停止计数
        return count

    def should_converge(self, threshold: float = 0.005,
                        max_rounds: int = 2) -> bool:
        """检查是否应该收敛停止"""
        return self.rounds_without_improvement(threshold) >= max_rounds

    # ── 查询 ────────────────────────────────────────────

    def get_keep_chain(self) -> list[dict]:
        """获取 keep 链 (从初始 baseline 到当前)"""
        kept = [e for e in self._state.lineage if e["decision"] == "keep"]
        kept.sort(key=lambda e: e["round"])
        return kept

    def get_excluded_summary(self) -> str:
        """生成排除方向的摘要文本 (用于注入 Ask)"""
        if not self._state.excluded_directions:
            return ""
        lines = ["\n已排除的无效方向:"]
        for d in self._state.excluded_directions[-5:]:  # 最近 5 个
            lines.append(
                f"  - {d['trial']} (Round {d['round']}): "
                f"WAPE delta={d.get('wape_delta', 0):+.4f}, {d.get('reason', '')}"
            )
        return "\n".join(lines)

    def get_lineage_tree(self) -> str:
        """生成追溯树的文本表示"""
        if not self._state.lineage:
            return "(空)"

        lines = []
        for e in self._state.lineage:
            symbol = {"keep": "├─", "reverse": "✕─", "rollback": "↻─"}.get(
                e["decision"], "?─"
            )
            parent = f"(← R{e['parent']})" if e.get("parent") is not None else "(root)"
            lines.append(
                f"  {symbol} R{e['round']} [{e['trial']}] "
                f"WAPE={e.get('wape', 0):.4f} {parent}"
            )
        return "\n".join(lines)

    # ── 重置 ────────────────────────────────────────────

    def reset(self) -> None:
        """重置所有状态 (删除 .loop_state.json 和所有 .checkpoint/)"""
        # 清理 checkpoint 目录
        for ckpt_dir in self.runs_dir.glob("*/.checkpoint"):
            shutil.rmtree(ckpt_dir)
        # 清理状态文件
        if self.state_file.exists():
            self.state_file.unlink()
        self._state = LoopState()
        print("[Checkpoint] 所有状态已重置")
