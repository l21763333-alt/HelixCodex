from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_servers.git_research_server import baseline_git_server as git_mcp


class GitMcpVisualFlowTest(unittest.TestCase):
    def test_diff_and_apply_trial_code_with_action_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline_dir = root / "repo" / "baseline"
            baseline_src = baseline_dir / "src"
            trial_code = root / "runs" / "trial_001" / "candidate" / "code"
            trial_src = trial_code / "src"
            action_log = root / "git_action_log.jsonl"

            baseline_src.mkdir(parents=True)
            trial_src.mkdir(parents=True)
            (baseline_src / "model.py").write_text("version = 'baseline'\n", encoding="utf-8")
            (baseline_src / "old_only.py").write_text("legacy = True\n", encoding="utf-8")
            (baseline_dir / "requirements.txt").write_text("lightgbm==4.0.0\n", encoding="utf-8")

            (trial_src / "model.py").write_text("version = 'trial'\n", encoding="utf-8")
            (trial_src / "extra.py").write_text("feature = 'new'\n", encoding="utf-8")
            (trial_code / "requirements.txt").write_text("lightgbm==4.1.0\n", encoding="utf-8")
            ctx = git_mcp.ModelRepoContext(
                repo_id="test",
                repo_path=root / "repo",
                model_root=baseline_dir,
                baseline_dir=baseline_dir,
                source_dir=baseline_dir,
                requirements=baseline_dir / "requirements.txt",
                requirements_paths=[baseline_dir / "requirements.txt"],
                copy_include=["**"],
                copy_exclude=["__pycache__/**", "*.pyc"],
                publish_paths=["baseline/**"],
                allowed_pathspecs=["baseline"],
                entrypoint_candidates=["train.py"],
                default_train_command=[],
                output_contract={},
                repo_url="",
                lifecycle="existing_worktree",
                sync_strategy="ff_only",
                publish_mode="direct_branch",
                branch_prefix="model-exp/",
                remote="origin",
                base_branch="main",
                push_target_branch="main",
                push_on_keep=False,
                create_pr_on_keep=False,
                pr_draft=True,
                require_human_approval_for_push=True,
            )

            def fake_log(action: str, payload: dict) -> None:
                action_log.parent.mkdir(parents=True, exist_ok=True)
                with action_log.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"action": action, "payload": payload}, ensure_ascii=False) + "\n")

            with (
                patch.object(git_mcp, "_repo_context", return_value=ctx),
                patch.object(git_mcp, "_ensure_model_paths_clean", return_value=None),
                patch.object(git_mcp, "_log", side_effect=fake_log),
            ):
                diff = git_mcp.diff_trial_model_code(trial_code)
                applied = git_mcp.apply_trial_to_baseline(trial_code, "trial_001")

            self.assertEqual(diff["changed"], 2)
            self.assertEqual(diff["added"], 1)
            self.assertEqual(diff["removed"], 1)
            self.assertIn("M src/model.py", diff["summary"])
            self.assertIn("D src/old_only.py", diff["summary"])
            self.assertIn("A src/extra.py", diff["summary"])
            self.assertIn("M requirements.txt", diff["summary"])

            self.assertEqual((baseline_src / "model.py").read_text(encoding="utf-8"), "version = 'trial'\n")
            self.assertEqual((baseline_src / "extra.py").read_text(encoding="utf-8"), "feature = 'new'\n")
            self.assertFalse((baseline_src / "old_only.py").exists())
            self.assertEqual(
                (baseline_dir / "requirements.txt").read_text(encoding="utf-8"),
                "lightgbm==4.1.0\n",
            )

            records = [json.loads(line) for line in action_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["action"] for record in records], [
                "diff_trial_model_code",
                "apply_trial_to_baseline",
            ])
            self.assertEqual(applied["trial_id"], "trial_001")
            self.assertEqual(applied["model_root"], str(baseline_dir))


if __name__ == "__main__":
    unittest.main()
