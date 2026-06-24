from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class FakePublishGit:
    def __init__(self, *, blocked: bool = False) -> None:
        self.blocked = blocked
        self.calls: list[tuple[str, str]] = []

    def sync_remote_base(self, remote: str, base_branch: str) -> dict:
        self.calls.append(("sync", f"{remote}/{base_branch}"))
        if self.blocked:
            return {
                "synced": False,
                "blocked": True,
                "reason": "baseline model code has uncommitted changes",
                "model_changes": ["baseline/src/model.py"],
                "remote": remote,
                "base_branch": base_branch,
            }
        return {
            "synced": True,
            "blocked": False,
            "remote": remote,
            "base_branch": base_branch,
            "remote_head": "remote-sha",
            "local_head_after": "local-sha",
            "branch_after": base_branch,
        }

    def apply_trial_to_baseline(self, trial_code_dir: Path, trial_id: str) -> dict:
        self.calls.append(("apply", trial_id))
        return {"trial_id": trial_id, "trial_code_dir": str(trial_code_dir)}

    def commit_baseline_model_update(self, trial_id: str, metrics: dict, report_path: str, supplement: str | None) -> dict:
        self.calls.append(("commit", trial_id))
        return {"committed": True, "trial_id": trial_id, "commit": "commit-sha"}

    def push_model_trial_branch(self, branch: str, remote: str, target_branch: str | None = None) -> dict:
        self.calls.append(("push", target_branch or branch))
        return {
            "pushed": True,
            "remote": remote,
            "branch": branch,
            "target_branch": target_branch or branch,
        }

    def create_model_pr(self, branch: str, base: str, body: str, title: str, draft: bool) -> dict:
        self.calls.append(("pr", branch))
        return {"created": False, "draft_path": "/fake/pr.md", "branch": branch, "base": base}

    def get_model_repo_state(self) -> dict:
        self.calls.append(("state", ""))
        return {"branch": "model-exp/run_trial_001", "head": "commit-sha", "model_dirty": False}


class GitPublishFlowTest(unittest.TestCase):
    def test_loop_start_sync_blocks_on_dirty_model_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            baseline.mkdir()
            runs = root / "runs"
            fake_git = FakePublishGit(blocked=True)

            import loop

            with (
                patch("builtins.print", lambda *_, **__: None),
                patch.object(loop, "baseline_git_mcp", fake_git),
                patch.object(loop, "git_subagent", None),
                patch.object(loop, "notify_loop_start", lambda *_, **__: True),
                patch.object(loop, "notify_git_sync_result", lambda *_, **__: True),
            ):
                agent_loop = loop.AIExperimentLoop(
                    experiment_dir=str(baseline),
                    ask="sync test",
                    output_base=str(runs),
                    max_iter=1,
                    human_review=False,
                )
                agent_loop._on_loop_start()

        self.assertTrue(agent_loop.should_stop)
        self.assertIn("Git baseline sync failed", agent_loop._stop_reason)
        self.assertIn(("sync", "forecastops/ForecastModel"), fake_git.calls)

    def test_keep_pushes_and_creates_pr_without_training(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            baseline.mkdir()
            trial = root / "runs" / "trial_001"
            (trial / "agent2" / "code" / "src").mkdir(parents=True)
            (trial / "final_report.md").write_text("# report\n", encoding="utf-8")
            fake_git = FakePublishGit()

            import loop

            with (
                patch("builtins.print", lambda *_, **__: None),
                patch.object(loop, "baseline_git_mcp", fake_git),
                patch.object(loop, "git_subagent", None),
                patch.object(loop, "notify_git_publish_result", lambda *_, **__: True),
                patch.object(loop, "notify_error", lambda *_, **__: True),
            ):
                agent_loop = loop.AIExperimentLoop(
                    experiment_dir=str(baseline),
                    ask="keep publish test",
                    output_base=str(root / "runs"),
                    max_iter=1,
                    human_review=False,
                )
                agent_loop._on_keep({
                    "trial_id": "trial_001",
                    "git_trial_id": "run_trial_001",
                    "output_dir": str(trial),
                    "comparison": {"primary": {"old_wape": 0.7, "new_wape": 0.6}},
                    "supplement": None,
                })

                actions = [name for name, _ in fake_git.calls]
                self.assertIn("apply", actions)
                self.assertIn("commit", actions)
                self.assertIn("push", actions)
                self.assertNotIn("pr", actions)
                self.assertIn(("push", "ForecastModel"), fake_git.calls)
                self.assertEqual(agent_loop.current_previous_trial, str(trial))
                self.assertTrue((trial / "git_publish_result.json").exists())

    def test_subagent_prompt_requires_git_mcp_only(self) -> None:
        import git_subagent

        prompts: list[str] = []

        def fake_run(prompt: str, *, cwd=None) -> dict:
            prompts.append(prompt)
            return {"ok": True, "publish": {}, "state": {}, "error": None}

        with patch.object(git_subagent, "_run_git_subagent", fake_run):
            result = git_subagent.publish_existing_keep_via_subagent(
                trial_id="run_trial_001",
                branch="model-exp/run_trial_001",
                metrics={},
                report_path="runs/trial_001/final_report.md",
                commit={"committed": True, "commit": "sha"},
            )

        self.assertTrue(result["ok"])
        self.assertIn("Use git_research MCP tools only", prompts[0])
        self.assertIn("Do not call shell", prompts[0])
        self.assertIn("Do not force push", prompts[0])


if __name__ == "__main__":
    unittest.main()
