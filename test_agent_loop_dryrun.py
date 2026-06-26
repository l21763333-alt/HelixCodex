from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class FakeBaselineGitMcp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def create_model_trial_branch(self, trial_id: str, **kwargs) -> dict:
        self.calls.append(("branch", trial_id))
        return {"branch": f"model-exp/{trial_id}"}

    def sync_remote_base(self, remote: str, base_branch: str, **kwargs) -> dict:
        self.calls.append(("sync", f"{remote}/{base_branch}"))
        return {
            "synced": True,
            "blocked": False,
            "remote": remote,
            "base_branch": base_branch,
            "remote_head": "fake-remote",
            "local_head_after": "fake-local",
            "branch_after": base_branch,
        }

    def snapshot_baseline_model(self, trial_id: str, **kwargs) -> dict:
        self.calls.append(("snapshot", trial_id))
        return {"snapshot_path": f"/fake/snapshots/{trial_id}"}

    def diff_trial_model_code(self, trial_code_dir: Path, **kwargs) -> dict:
        self.calls.append(("diff", trial_code_dir.as_posix()))
        return {
            "changed": 1,
            "added": 0,
            "removed": 0,
            "summary": "M src/model.py",
        }

    def apply_trial_to_baseline(self, trial_code_dir: Path, trial_id: str, **kwargs) -> dict:
        self.calls.append(("apply", trial_id))
        return {"trial_id": trial_id, "trial_code_dir": str(trial_code_dir)}

    def commit_baseline_model_update(
        self,
        trial_id: str,
        metrics: dict,
        report_path: str,
        supplement: str | None,
        **kwargs,
    ) -> dict:
        self.calls.append(("commit", trial_id))
        return {"committed": True, "trial_id": trial_id, "commit": "fake-sha"}

    def push_model_trial_branch(self, branch: str, remote: str, target_branch: str | None = None, **kwargs) -> dict:
        self.calls.append(("push", target_branch or branch))
        return {"pushed": True, "branch": branch, "remote": remote, "target_branch": target_branch or branch}

    def create_model_pr(self, branch: str, base: str, body: str, title: str, draft: bool, **kwargs) -> dict:
        self.calls.append(("pr", branch))
        return {"created": False, "branch": branch, "base": base, "draft_path": "/fake/pr.md"}

    def get_model_repo_state(self, **kwargs) -> dict:
        self.calls.append(("state", ""))
        return {"branch": "model-exp/fake", "head": "fake-sha", "model_dirty": False}

    def discard_unaccepted_model_changes(self, trial_id: str, **kwargs) -> dict:
        self.calls.append(("discard", trial_id))
        return {"trial_id": trial_id, "discarded_tracked": True}


class AgentLoopDryRunTest(unittest.TestCase):
    def test_loop_restores_keep_checkpoint_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            baseline.mkdir()
            runs = root / "runs"
            trial = runs / "trial_001"
            (trial / "agent2" / "code").mkdir(parents=True)
            runs.mkdir(exist_ok=True)
            (runs / ".loop_state.json").write_text(
                json.dumps({
                    "current_round": 1,
                    "current_baseline_trial": "trial_001",
                    "lineage": [{
                        "round": 1,
                        "trial": "trial_001",
                        "decision": "keep",
                        "parent": None,
                        "wape": 0.61,
                        "wape_delta": 0.08,
                        "human_supplement": "try weather feature",
                        "timestamp": "2026-06-24T00:00:00",
                    }],
                    "excluded_directions": [],
                    "rollback_count": {},
                    "consecutive_reverses": 0,
                    "created_at": "",
                    "updated_at": "2026-06-24T00:00:00",
                }),
                encoding="utf-8",
            )

            import loop

            with patch("builtins.print", lambda *_, **__: None):
                agent_loop = loop.AIExperimentLoop(
                    experiment_dir=str(baseline),
                    ask="resume dry-run",
                    output_base=str(runs),
                    max_iter=3,
                    human_review=False,
                )

            self.assertEqual(agent_loop.round_num, 1)
            self.assertEqual(agent_loop.trial_counter, 1)
            self.assertEqual(agent_loop.current_previous_trial, str(trial))
            self.assertIn("try weather feature", agent_loop.current_ask)

    def test_loop_rollback_then_keep_without_training(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            (baseline / "src").mkdir(parents=True)
            (baseline / "src" / "model.py").write_text("BASELINE = True\n", encoding="utf-8")
            (baseline / "requirements.txt").write_text("pandas\n", encoding="utf-8")
            runs = root / "runs"

            decisions = iter([
                ("rollback", "try another feature"),
                ("keep", "accepted"),
            ])

            workflow_calls: list[tuple[str, str | None, str]] = []

            def fake_run_workflow(
                experiment_dir: str,
                ask: str,
                output_dir: str,
                *,
                previous_trial: str | None = None,
            ):
                workflow_calls.append((experiment_dir, previous_trial, output_dir))
                out = Path(output_dir)
                (out / "evaluation").mkdir(parents=True)
                (out / "agent2" / "code" / "src").mkdir(parents=True)
                (out / "agent2" / "code" / "src" / "model.py").write_text(
                    f"# fake optimized model from {Path(output_dir).name}\n",
                    encoding="utf-8",
                )
                (out / "agent2" / "code" / "requirements.txt").write_text(
                    "pandas\n",
                    encoding="utf-8",
                )
                (out / "final_report.md").write_text("# fake report\n", encoding="utf-8")
                comparison = {
                    "decision": "keep",
                    "wape_delta": 0.02,
                    "bias_delta": 0.0,
                    "reason": "dry-run metric",
                    "primary": {
                        "old_wape": 0.70,
                        "new_wape": 0.68,
                        "old_bias": 0.10,
                        "new_bias": 0.09,
                    },
                }
                (out / "evaluation" / "metric_comparison.json").write_text(
                    json.dumps(comparison),
                    encoding="utf-8",
                )
                return types.SimpleNamespace(output_dir=str(out))

            fake_codex_flow = types.SimpleNamespace(run_workflow=fake_run_workflow)
            fake_git = FakeBaselineGitMcp()

            with patch.dict(sys.modules, {"codex_flow": fake_codex_flow}):
                import loop

                with (
                    patch("builtins.print", lambda *_, **__: None),
                    patch.object(loop, "baseline_git_mcp", fake_git),
                    patch.object(loop, "git_subagent", None),
                    patch.object(loop, "feishu_review_via_mcp", lambda **_: next(decisions)),
                    patch.object(loop, "human_review_enabled", lambda: True),
                    patch.object(loop, "notify_loop_start", lambda *_, **__: True),
                    patch.object(loop, "notify_loop_stop", lambda *_, **__: True),
                    patch.object(loop, "notify_error", lambda *_, **__: True),
                    patch.object(loop, "notify_command_result", lambda *_, **__: True),
                ):
                    agent_loop = loop.AIExperimentLoop(
                        experiment_dir=str(baseline),
                        ask="dry-run the agent loop",
                        output_base=str(runs),
                        max_iter=1,
                        human_review=True,
                    )
                    result = agent_loop.run()

            actions = [name for name, _ in fake_git.calls]
            self.assertIn("discard", actions)
            self.assertIn("apply", actions)
            self.assertIn("commit", actions)
            self.assertEqual(result["total_rounds"], 1)
            self.assertEqual(workflow_calls[0][0], str(baseline))
            self.assertIsNone(workflow_calls[0][1])
            self.assertEqual(workflow_calls[1][0], str(baseline))
            self.assertIsNone(workflow_calls[1][1])
            self.assertTrue((runs / "trial_001" / ".checkpoint" / "snapshot.json").exists())
            self.assertTrue((runs / "trial_002" / ".checkpoint" / "snapshot.json").exists())


if __name__ == "__main__":
    unittest.main()
